#!/usr/bin/env python3
"""Friday RC → 1.0 acceptance wrapper.

This is not a second quality-gate controller and cannot turn a red canonical
verdict green. It loads the capability matrix, classifies discovered surfaces,
audits sealed A/B inventories, runs harness self-tests, and prints the exact
diagnostic-baseline / final commands that still belong to the existing tools.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = Path(__file__).with_name("release_1_0_capability_matrix.json")
SCHEMA = "friday.release-1-0-acceptance.v1"
MATRIX_SCHEMA = "friday.release-1-0-capability-matrix.v1"
WRAPPER_RELATIVE = "tools/release_1_0_acceptance.py"
CANONICAL_TOOLS = (
    "tools/quality_gate.py",
    "tools/synthetic_live_acceptance.py",
    "tools/synthetic_live_battery.py",
    "tools/synthetic_live_b09_evidence.py",
    "tools/synthetic_live_b09_lab.py",
    "tools/document_contour_live_battery.py",
)
OBLIGATIONS = frozenset({"required", "optional_when_enabled", "beta", "out_of_scope"})
LAYERS = frozenset({"deterministic", "isolated-live", "user-ui", "deployment-device", "harness"})
EXECUTION_DRIVERS = {
    "journey": {"deterministic"},
    "canonical-pytest": {"deterministic", "user-ui"},
    "native": {"isolated-live"},
    "app-soak": {"isolated-live"},
    "telegram-roundtrip": {"deployment-device"},
    "harness": {"harness"},
    "unimplemented": LAYERS,
}
# Existing pytest cases execute once in the canonical inventory/gate. This is a
# registration contract, not another test dispatcher or a collection-as-PASS path.
PYTEST_CASE_BINDINGS = {
    "R10-TELEGRAM-RESEARCH-CODING": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_telegram_bridge_exposes_modes_inbox_and_feedback_callbacks",
            "tests/test_coding_mode_surface.py::test_telegram_coding_command_sets_the_mode",
            "tests/test_coding_mode_surface.py::test_telegram_coding_command_explains_owner_only_403",
        ),
        (
            "telegram:research",
            "telegram:coding",
        ),
    ),
    "R10-TELEGRAM-INBOX": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_telegram_bridge_exposes_modes_inbox_and_feedback_callbacks",
        ),
        ("telegram:inbox",),
    ),
    "R10-TELEGRAM-CONFLICTS": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_conflicts_command_lists_and_decides_via_backend",
            "tests/test_conflict_queue_triage_hints.py::test_telegram_conflicts_show_triage_label",
        ),
        ("telegram:conflicts",),
    ),
    "R10-TELEGRAM-RELATIONS": (
        "deterministic",
        (
            "tests/test_relation_triage_in_chat.py::test_relations_command_is_bounded_markup_safe_and_buttons_belong_to_invoker",
            "tests/test_relation_triage_in_chat.py::test_relations_command_never_sends_an_oversized_callback",
        ),
        ("telegram:relations",),
    ),
    "R10-TELEGRAM-MERGES": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_merges_command_lists_candidates_and_accept_rejects_via_backend",
            "tests/test_telegram_and_profile.py::test_merges_command_reports_when_no_candidates",
        ),
        ("telegram:merges",),
    ),
    "R10-TELEGRAM-SEARCH": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_search_command_lists_knowledge_without_llm",
            "tests/test_telegram_and_profile.py::test_search_command_without_query_shows_usage",
            "tests/test_telegram_and_profile.py::test_search_command_reports_no_matches",
        ),
        ("telegram:search",),
    ),
    "R10-TELEGRAM-SOURCE": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_source_command_searches_raw_files",
            "tests/test_telegram_and_profile.py::test_source_command_omits_an_oversized_document_callback",
            "tests/test_telegram_and_profile.py::test_source_command_keeps_boundary_button_numbering_aligned",
        ),
        ("telegram:source",),
    ),
    "R10-TELEGRAM-BROWSE-TAGS": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_tags_command_lists_tags_with_counts",
            "tests/test_telegram_and_profile.py::test_browse_command_by_tag_then_entity_fallback_and_tree",
            "tests/test_telegram_and_profile.py::test_browse_with_namesakes_offers_a_choice",
        ),
        (
            "telegram:browse",
            "telegram:tags",
        ),
    ),
    "R10-TELEGRAM-PROFILE": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_profile_command_shows_the_object_view",
            "tests/test_telegram_and_profile.py::test_profile_command_shows_when_an_event_occurred",
            "tests/test_telegram_and_profile.py::test_profile_command_reports_not_found_honestly",
            "tests/test_bridge_tells_refusal_from_absence.py::test_a_forbidden_profile_is_not_reported_as_an_empty_archive",
            "tests/test_bridge_tells_refusal_from_absence.py::test_a_missing_profile_still_says_it_is_missing",
        ),
        ("telegram:profile",),
    ),
    "R10-TELEGRAM-ENTITY-RENAME-ALIAS": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_renaming_an_object_keeps_names_with_spaces_whole",
            "tests/test_telegram_and_profile.py::test_rename_without_the_arrow_explains_the_format",
            "tests/test_telegram_and_profile.py::test_an_alias_helps_search_without_merging_anything",
            "tests/test_telegram_and_profile.py::test_a_duplicate_alias_is_not_added_twice",
            "tests/test_telegram_and_profile.py::test_an_alias_that_is_another_object_is_refused_not_silently_merged",
            "tests/test_telegram_and_profile.py::test_a_differently_spelled_duplicate_alias_is_recognised",
            "tests/test_bridge_tells_refusal_from_absence.py::test_an_alias_is_not_added_when_the_clash_check_could_not_run",
        ),
        (
            "telegram:entity_rename",
            "telegram:entity_alias",
        ),
    ),
    "R10-TELEGRAM-WHY": (
        "deterministic",
        (
            "tests/test_telegram_and_profile.py::test_why_before_any_answer_explains_itself_instead_of_dead_letter",
            "tests/test_telegram_and_profile.py::test_why_names_its_sources_instead_of_bare_labels",
        ),
        ("telegram:why",),
    ),
    "R10-TELEGRAM-NOTE": (
        "deterministic",
        ("tests/test_telegram_and_profile.py::test_note_command_marks_the_turn_as_an_explicit_save",),
        ("telegram:note",),
    ),
    "R10-TELEGRAM-RETRY": (
        "deterministic",
        (
            "tests/test_telegram_update_completion_fence.py::test_retry_command_caches_one_response_and_resumes_its_file_suffix",
        ),
        ("telegram:retry",),
    ),
    "R10-TELEGRAM-SELFCONV": (
        "deterministic",
        (
            "tests/test_conversation_self_service_telegram.py::test_archive_delete_rename_commands_hit_current_conversation",
            "tests/test_conversation_self_service_telegram.py::test_a_different_group_member_cannot_confirm_someone_elses_delete",
            "tests/test_conversation_self_service_telegram.py::test_delete_keep_callback_does_not_call_backend",
            "tests/test_conversation_export.py::test_export_command_calls_send_document",
        ),
        (
            "telegram:archive",
            "telegram:delete",
            "telegram:rename",
            "telegram:export",
        ),
    ),
    "R10-TELEGRAM-OBSIDIAN": (
        "deterministic",
        (
            "tests/test_telegram_obsidian.py::test_obsidian_is_private_resumable_and_copy_text_is_the_exact_first_button",
            "tests/test_telegram_obsidian.py::test_disabled_obsidian_is_hidden_and_never_calls_the_backend",
            "tests/test_telegram_obsidian.py::test_unicode_alias_command_is_private_owner_signed_and_exact",
        ),
        (
            "telegram:obsidian",
            "telegram:obsidian_alias",
        ),
    ),
    "R10-TELEGRAM-COMMAND-GAPS": (
        "deterministic",
        (
            "tests/test_release_1_0_telegram_command_gaps.py::test_mode_commands_reach_exact_backend_and_label[chat]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_mode_commands_reach_exact_backend_and_label[work]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_mode_commands_reach_exact_backend_and_label[engineer]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_engineer_disabled_is_local_and_never_calls_backend",
            "tests/test_release_1_0_telegram_command_gaps.py::test_engineer_forbidden_is_named_and_keeps_person_chat_context",
            "tests/test_release_1_0_telegram_command_gaps.py::test_history_without_query_is_usage_only",
            "tests/test_release_1_0_telegram_command_gaps.py::test_history_reaches_own_message_search_and_renders_result[empty]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_history_reaches_own_message_search_and_renders_result[hits]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_timeline_rejects_unparsed_period_without_guessing",
            "tests/test_release_1_0_telegram_command_gaps.py::test_timeline_default_reaches_both_views_and_emits_document_button",
            "tests/test_release_1_0_telegram_command_gaps.py::test_graph_without_pair_is_usage_only",
            "tests/test_release_1_0_telegram_command_gaps.py::test_graph_reaches_path_and_renders_direction",
            "tests/test_release_1_0_telegram_command_gaps.py::test_graph_not_found_names_bound_and_no_fake_path",
            "tests/test_release_1_0_telegram_command_gaps.py::test_graph_forbidden_is_not_reported_as_absence",
            "tests/test_release_1_0_telegram_command_gaps.py::test_watch_without_topic_is_usage_only",
            "tests/test_release_1_0_telegram_command_gaps.py::test_watch_creates_exact_person_monitor",
            "tests/test_release_1_0_telegram_command_gaps.py::test_watching_lists_and_stops_exact_monitor",
            "tests/test_release_1_0_telegram_command_gaps.py::test_watching_empty_is_honest",
            "tests/test_release_1_0_telegram_command_gaps.py::test_watch_is_refused_before_backend_for_unallowed_chat",
            "tests/test_release_1_0_telegram_command_gaps.py::test_approvals_private_lists_pending_and_uncertain_with_buttons",
            "tests/test_release_1_0_telegram_command_gaps.py::test_approval_decision_callbacks_reach_exact_action[approve]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_approval_decision_callbacks_reach_exact_action[reject]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_mission_without_goal_is_usage_and_registration_only",
            "tests/test_release_1_0_telegram_command_gaps.py::test_mission_create_reaches_backend_and_renders_plan",
            "tests/test_release_1_0_telegram_command_gaps.py::test_missions_list_reaches_view_and_start_callback",
            "tests/test_release_1_0_telegram_command_gaps.py::test_compact_reaches_exact_view[empty]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_compact_reaches_exact_view[incident]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_status_reaches_backend_and_renders_exact_counts",
            "tests/test_release_1_0_telegram_command_gaps.py::test_new_resets_exact_channel_and_preserves_knowledge_message",
            "tests/test_release_1_0_telegram_command_gaps.py::test_instructions_show_saved_value",
            "tests/test_release_1_0_telegram_command_gaps.py::test_instructions_mutations_reach_exact_self_route[set]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_instructions_mutations_reach_exact_self_route[clear]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_help_reflects_optional_surfaces_exactly[optional-off]",
            "tests/test_release_1_0_telegram_command_gaps.py::test_help_reflects_optional_surfaces_exactly[optional-on]",
            "tests/test_bridge_tells_refusal_from_absence.py::test_approvals_are_not_listed_into_a_group",
        ),
        (
            "telegram:chat",
            "telegram:work",
            "telegram:engineer",
            "telegram:history",
            "telegram:timeline",
            "telegram:graph",
            "telegram:watch",
            "telegram:watching",
            "telegram:approvals",
            "telegram:mission",
            "telegram:missions",
            "telegram:status",
            "telegram:compact",
            "telegram:new",
            "telegram:instructions",
            "telegram:help",
        ),
    ),
    "R10-EVAL-SIGNAL-ABLATION-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_eval_comparisons_http.py::test_ablation_http_real_search_repeat_and_scoped_receipt[owner]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_ablation_http_real_search_repeat_and_scoped_receipt[admin]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_empty_gold_refuses_a_metric[ablation]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_auth_refuses_before_engine[anonymous-401-ablation]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_auth_refuses_before_engine[user-403-ablation]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_disabled_arm_keeps_prior_receipts[ablation-no ablatable signals]",
        ),
        ("api:POST /api/admin/eval/ablation",),
    ),
    "R10-EVAL-CHUNK-AB-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_eval_comparisons_http.py::test_chunk_ab_http_real_index_search_gain_and_scoped_delta[owner]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_chunk_ab_http_real_index_search_gain_and_scoped_delta[admin]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_empty_gold_refuses_a_metric[chunk-ab]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_auth_refuses_before_engine[anonymous-401-chunk-ab]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_auth_refuses_before_engine[user-403-chunk-ab]",
            "tests/test_release_1_0_eval_comparisons_http.py::test_eval_comparison_http_disabled_arm_keeps_prior_receipts[chunk-ab-chunking disabled]",
        ),
        ("api:POST /api/admin/eval/chunk-ab",),
    ),
    "R10-INBOX-CLASSIFY-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_inbox_enrichment_http.py::test_personal_classify_persists_exact_review_feedback_and_repeat",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[personal-anonymous-401-Missing authentication]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[personal-nonprivileged-403-Access denied for inbox.review (default_deny)]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[personal-foreign-404-\\u042d\\u043b\\u0435\\u043c\\u0435\\u043d\\u0442 \\u0432\\u0445\\u043e\\u0434\\u044f\\u0449\\u0438\\u0445 \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[personal-missing-404-\\u042d\\u043b\\u0435\\u043c\\u0435\\u043d\\u0442 \\u0432\\u0445\\u043e\\u0434\\u044f\\u0449\\u0438\\u0445 \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[personal-bad-status-400-\\u041d\\u0435\\u0434\\u043e\\u043f\\u0443\\u0441\\u0442\\u0438\\u043c\\u044b\\u0439 \\u0441\\u0442\\u0430\\u0442\\u0443\\u0441 \\u0432\\u0445\\u043e\\u0434\\u044f\\u0449\\u0438\\u0445]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[personal-malformed-json-400-\\u0422\\u0435\\u043b\\u043e \\u0437\\u0430\\u043f\\u0440\\u043e\\u0441\\u0430 \\u0434\\u043e\\u043b\\u0436\\u043d\\u043e \\u0431\\u044b\\u0442\\u044c \\u043a\\u043e\\u0440\\u0440\\u0435\\u043a\\u0442\\u043d\\u044b\\u043c JSON]",
        ),
        ("api:POST /api/inbox/{inbox_id}/classify",),
    ),
    "R10-ADMIN-INBOX-CLASSIFY-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_inbox_enrichment_http.py::test_admin_classify_promotes_one_object_and_versions_the_repeat",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-classify-anonymous-401-Missing authentication]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-classify-nonprivileged-403-Access denied for admin.all_data.manage (default_deny)]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-classify-foreign-404-\\u042d\\u043b\\u0435\\u043c\\u0435\\u043d\\u0442 \\u0432\\u0445\\u043e\\u0434\\u044f\\u0449\\u0438\\u0445 \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-classify-missing-404-\\u042d\\u043b\\u0435\\u043c\\u0435\\u043d\\u0442 \\u0432\\u0445\\u043e\\u0434\\u044f\\u0449\\u0438\\u0445 \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-classify-bad-importance-400-importance: \\u043d\\u0443\\u0436\\u043d\\u043e \\u0447\\u0438\\u0441\\u043b\\u043e]",
        ),
        ("api:POST /api/admin/inbox/{inbox_id}/classify",),
    ),
    "R10-ADMIN-INBOX-ADVISE-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_inbox_enrichment_http.py::test_admin_advise_pins_final_model_input_advisory_state_and_replay",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_admin_advise_malformed_and_failed_model_responses_are_effect_free",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-anonymous-401-Missing authentication]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-nonprivileged-403-Access denied for admin.all_data.manage (default_deny)]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-foreign-400-Inbox item not found]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-missing-400-Inbox item not found]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-unknown-user-404-\\u041f\\u043e\\u043b\\u044c\\u0437\\u043e\\u0432\\u0430\\u0442\\u0435\\u043b\\u044c \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-bad-force-400-force: \\u043d\\u0443\\u0436\\u043d\\u043e \\u043b\\u043e\\u0433\\u0438\\u0447\\u0435\\u0441\\u043a\\u043e\\u0435 \\u0437\\u043d\\u0430\\u0447\\u0435\\u043d\\u0438\\u0435]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[admin-advise-disabled-503-\\u041b\\u043e\\u043a\\u0430\\u043b\\u044c\\u043d\\u0430\\u044f \\u043c\\u043e\\u0434\\u0435\\u043b\\u044c \\u043e\\u0442\\u043a\\u043b\\u044e\\u0447\\u0435\\u043d\\u0430]",
        ),
        ("api:POST /api/admin/inbox/{inbox_id}/advise",),
    ),
    "R10-KNOWLEDGE-REENRICH-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[reenrich-anonymous-401-Missing authentication]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[reenrich-nonprivileged-403-Access denied for admin.all_data.manage (default_deny)]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[reenrich-foreign-404-\\u041e\\u0431\\u044a\\u0435\\u043a\\u0442 \\u0437\\u043d\\u0430\\u043d\\u0438\\u044f \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[reenrich-missing-404-\\u041e\\u0431\\u044a\\u0435\\u043a\\u0442 \\u0437\\u043d\\u0430\\u043d\\u0438\\u044f \\u043d\\u0435 \\u043d\\u0430\\u0439\\u0434\\u0435\\u043d]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_enrichment_refusals_precede_provider_and_preserve_durable_state[reenrich-bad-apply-400-apply: \\u043d\\u0443\\u0436\\u043d\\u043e \\u043b\\u043e\\u0433\\u0438\\u0447\\u0435\\u0441\\u043a\\u043e\\u0435 \\u0437\\u043d\\u0430\\u0447\\u0435\\u043d\\u0438\\u0435]",
            "tests/test_release_1_0_inbox_enrichment_http.py::test_reenrich_preview_apply_and_repeat_preserve_provenance_and_lineage",
        ),
        ("api:POST /api/admin/knowledge/{knowledge_id}/reenrich",),
    ),
    "R10-DOCUMENT-MAP-OBSERVE-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_issue_consume_and_durable_exact_replay",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[anonymous-401-observe-shadow]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[user-403-observe-shadow]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[admin-403-observe-shadow]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[scoped-owner-403-observe-shadow]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_observe_body_refused_before_witness[object]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_observe_body_refused_before_witness[null]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_observe_body_refused_before_witness[space]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_observe_body_refused_before_witness[text]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_failed_receipt_burns_attempt",
        ),
        ("api:POST /api/admin/secondary-document-map-witness/observe-shadow",),
    ),
    "R10-DOCUMENT-MAP-CONSUME-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_issue_consume_and_durable_exact_replay",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_refusal_preserves_witness[lookup]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_refusal_preserves_witness[attestation]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_refusal_preserves_witness[receipt]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_refusal_preserves_witness[process]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_refusal_preserves_witness[expired]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_refusal_preserves_witness[rebind]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[anonymous-401-consume-rollout-attestation]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[user-403-consume-rollout-attestation]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[admin-403-consume-rollout-attestation]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_owner_token_guard_before_witness[scoped-owner-403-consume-rollout-attestation]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_bad_body_before_witness[invalid-json]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_bad_body_before_witness[array]",
            "tests/test_release_1_0_document_map_witness_http.py::test_document_map_http_consume_bad_body_before_witness[missing-fields]",
        ),
        ("api:POST /api/admin/secondary-document-map-witness/consume-rollout-attestation",),
    ),
    "R10-SEMANTIC-WITNESS-ISSUE-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issues_recomputes_signs_consumes_and_replays[assist]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issues_recomputes_signs_consumes_and_replays[canary]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issue_rejects_unbound_or_stale_candidate[baseline-hash]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issue_rejects_unbound_or_stale_candidate[registry]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issue_rejects_unbound_or_stale_candidate[population-drift]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issue_rejects_unbound_or_stale_candidate[latency-budget]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[anonymous-401-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[user-403-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[admin-403-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[scoped-owner-403-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_malformed_body_before_services[bad-json-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_malformed_body_before_services[array-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_malformed_body_before_services[missing-fields-issue-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_unavailable_runtime_refuses_after_refresh[issue-representative-window-attestation]",
        ),
        ("api:POST /api/admin/semantic-supervisor-witness/issue-representative-window-attestation",),
    ),
    "R10-SEMANTIC-WITNESS-CONSUME-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issues_recomputes_signs_consumes_and_replays[assist]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_issues_recomputes_signs_consumes_and_replays[canary]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_consume_refusal_preserves_exact_witness[lookup]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_consume_refusal_preserves_exact_witness[digest]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_consume_refusal_preserves_exact_witness[binding]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_consume_refusal_preserves_exact_witness[expired]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_consume_refusal_preserves_exact_witness[process]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_consume_refusal_preserves_exact_witness[rebind]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[anonymous-401-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[user-403-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[admin-403-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_owner_token_gate_before_services[scoped-owner-403-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_malformed_body_before_services[bad-json-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_malformed_body_before_services[array-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_malformed_body_before_services[missing-fields-consume-representative-window-attestation]",
            "tests/test_release_1_0_semantic_witness_http.py::test_semantic_http_unavailable_runtime_refuses_after_refresh[consume-representative-window-attestation]",
        ),
        ("api:POST /api/admin/semantic-supervisor-witness/consume-representative-window-attestation",),
    ),
    "R10-MISSION-START-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_exact_transition_author_audit_and_repeat[person-False]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_exact_transition_author_audit_and_repeat[person-True]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_exact_transition_author_audit_and_repeat[agent-False]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_exact_transition_author_audit_and_repeat[agent-True]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_owner_can_control_another_author_in_shared_archive",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_preserves_already_active_and_terminal_states[ready]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_preserves_already_active_and_terminal_states[running]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_preserves_already_active_and_terminal_states[completed]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_preserves_already_active_and_terminal_states[failed]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_preserves_already_active_and_terminal_states[cancelled]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_autonomy_disabled_blocks_without_running_a_task",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_refuses_before_executor_mutation[anonymous]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_refuses_before_executor_mutation[capability]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_refuses_before_executor_mutation[foreign-author]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_refuses_before_executor_mutation[foreign-tenant]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_mission_start_refuses_before_executor_mutation[missing]",
        ),
        ("api:POST /api/missions/{mission_id}/start",),
    ),
    "R10-RESEARCH-CANDIDATE-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_exact_provenance_review_only_and_repeat[False]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_exact_provenance_review_only_and_repeat[True]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[anonymous]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[capability]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[missing-id]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[missing-row]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[foreign]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[user-message]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[dialogue]",
            "tests/test_release_1_0_mission_research_actions_http.py::test_research_queue_refuses_before_ingestion_without_effects[knowledge-work]",
        ),
        ("api:POST /api/research/candidates",),
    ),
    "R10-API-SECONDARY-PRODUCT-WITNESS-LIFECYCLE": (
        "deterministic",
        (
            "tests/test_secondary_product_witness.py::test_runner_recovers_lost_ingest_and_cleanup_responses_through_real_api",
            "tests/test_secondary_product_witness.py::test_force_review_admin_advice_and_purge_leave_no_product_material",
            "tests/test_secondary_product_witness.py::test_reserved_witness_routes_reject_scoped_token_without_storage_rows[False]",
            "tests/test_secondary_product_witness.py::test_reserved_witness_routes_reject_scoped_token_without_storage_rows[True]",
            "tests/test_secondary_product_witness.py::test_witness_purge_refuses_raw_or_inbox_feedback_dependencies[raw-False]",
            "tests/test_secondary_product_witness.py::test_witness_purge_refuses_raw_or_inbox_feedback_dependencies[raw-True]",
            "tests/test_secondary_product_witness.py::test_witness_purge_refuses_raw_or_inbox_feedback_dependencies[inbox-False]",
            "tests/test_secondary_product_witness.py::test_witness_purge_refuses_raw_or_inbox_feedback_dependencies[inbox-True]",
        ),
        (
            "api:POST /api/admin/secondary-product-witness/consume-rollout-attestation",
            "api:POST /api/admin/secondary-product-witness/purge",
        ),
    ),
    "R10-MIXED-DELIVERY-HTTP": (
        "deterministic",
        (
            "tests/test_api_mixed_journey_runtime.py::test_http_mixed_upload_uses_real_runtime_and_replay_rechecks_archive[None-False]",
            "tests/test_api_mixed_journey_runtime.py::test_http_mixed_upload_uses_real_runtime_and_replay_rechecks_archive[missing_context-False]",
            "tests/test_api_mixed_journey_runtime.py::test_http_mixed_upload_uses_real_runtime_and_replay_rechecks_archive[changed_mode-False]",
            "tests/test_api_mixed_journey_runtime.py::test_http_mixed_upload_uses_real_runtime_and_replay_rechecks_archive[missing_identity-False]",
            "tests/test_api_mixed_journey_runtime.py::test_http_mixed_upload_uses_real_runtime_and_replay_rechecks_archive[None-True]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_preserves_exact_owner_and_bridge_projection",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[anonymous]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[chat-denied]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[sha-short]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[sha-charset]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[sha-missing]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[wrong-digest]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[foreign-bearer]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[foreign-bridge]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[missing-message]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[user-message]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[archive-revoked]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun[files-denied]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[owner-number]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[owner-extra]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[bridge-missing]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[bridge-wrong-file]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[bridge-wrong-turn]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[false-authority]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[wrong-status]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes[message-write]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[facts-schema]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[facts-extra]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[file-extra]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[archive-bool]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[facts-turn]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[web-extra]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[web-count-bool]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[web-id-private]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[web-state]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[web-turn]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_unclosed_durable_projection[format]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[override-mixed]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[override-ordinary]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[status-mixed]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[status-ordinary]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[preset-mixed]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[preset-ordinary]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[custom-grant-mixed]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_rechecks_chat_after_authentication[custom-grant-ordinary]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_changes_after_source_preparation[projection]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_changes_after_source_preparation[source-receipt]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_refuses_changes_after_source_preparation[chat-override]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_retains_closed_partial_web_delivery[consumable_degraded]",
            "tests/test_release_1_0_mixed_delivery_http.py::test_mixed_delivery_http_retains_closed_partial_web_delivery[unavailable]",
        ),
        ("api:GET /api/me/mixed-deliveries/{message_id}",),
    ),
    "R10-REGENERATE-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_real_runtime_replays_exact_last_user_and_appends_lineage",
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_operation_replay_is_effect_free_and_new_operation_is_alternative",
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_shared_archive_keeps_conversations_person_owned",
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_missing_auth_stops_before_agent_and_idempotency",
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_explicit_chat_deny_stops_before_agent_and_idempotency",
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_missing_conversation_stops_before_agent_and_idempotency",
            "tests/test_release_1_0_regenerate_http.py::test_regenerate_http_empty_conversation_stops_before_agent_and_idempotency",
        ),
        ("api:POST /api/me/regenerate",),
    ),
    "R10-API-BACKUP-VERIFY-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_real_database_exact_receipt_private_audit_and_repeat[owner]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_real_database_exact_receipt_private_audit_and_repeat[admin]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_real_corruption_is_false_and_preserves_copies[wrong-digest]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_real_corruption_is_false_and_preserves_copies[missing-manifest]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_real_corruption_is_false_and_preserves_copies[invalid-schema]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_bad_backup_refuses_404_without_success_audit[missing]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_bad_backup_refuses_404_without_success_audit[symlink]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_bad_backup_refuses_404_without_success_audit[backslash]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_bad_backup_refuses_404_without_success_audit[wrong-suffix]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_requires_backup_capability_before_provider[anonymous-401]",
            "tests/test_release_1_0_backup_verify_http.py::test_http_verify_requires_backup_capability_before_provider[user-403]",
        ),
        ("api:POST /api/admin/backups/{filename}/verify",),
    ),
    "R10-CLI-SERVER-PROCESS": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_server_process.py::test_cli_real_server_ready_singleton_and_graceful_shutdown[server]",
        ),
        ("cli:server",),
    ),
    "R10-CLI-UP-PROCESS": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_server_process.py::test_cli_real_server_ready_singleton_and_graceful_shutdown[up]",
        ),
        ("cli:up",),
    ),
    "R10-CLI-TUI-BODY": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_tui_body.py::test_cli_tui_quit_runs_real_body_and_returns_through_wrapper",
            "tests/test_release_1_0_cli_tui_body.py::test_cli_tui_dirty_start_saves_private_env_before_exact_up_exec",
            "tests/test_release_1_0_cli_tui_body.py::test_cli_tui_failed_autosave_never_execs_up",
            "tests/test_release_1_0_cli_tui_body.py::test_cli_tui_exec_failure_returns_redacted_exit_and_clean_wrapper",
            "tests/test_release_1_0_cli_tui_body.py::test_cli_tui_real_owned_pty_quit_restores_termios",
        ),
        ("cli:tui",),
    ),
    "R10-CLI-TELEGRAM-BRIDGE-BODY": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_real_run_releases_queue_clients_lease_and_tasks[normal]",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_real_run_releases_queue_clients_lease_and_tasks[loop_error]",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_real_run_releases_queue_clients_lease_and_tasks[client_error]",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_held_lease_refuses_before_queue_or_http",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_invalid_config_refuses_without_queue_or_client[token]",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_invalid_config_refuses_without_queue_or_client[allowlist]",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_invalid_config_refuses_without_queue_or_client[backend_url]",
            "tests/test_release_1_0_cli_bridge_body.py::test_cli_bridge_real_invalid_ca_releases_inbox_and_kernel_lease",
        ),
        ("cli:telegram-bridge",),
    ),
    "R10-SERVER-STARTUP-RESOURCES": (
        "deterministic",
        (
            "tests/test_release_1_0_server_startup_resources.py::test_server_startup_exit_permanently_closes_storage_before_releasing_role[normal]",
            "tests/test_release_1_0_server_startup_resources.py::test_server_startup_exit_permanently_closes_storage_before_releasing_role[workers]",
            "tests/test_release_1_0_server_startup_resources.py::test_server_startup_exit_permanently_closes_storage_before_releasing_role[mcp]",
        ),
        ("cli:server",),
    ),
    "R10-SUPERVISOR-STARTUP-RESOURCES": (
        "deterministic",
        (
            "tests/test_release_1_0_supervisor_startup.py::test_supervisor_initial_popen_refusal_closes_log_and_restores_signals",
            "tests/test_release_1_0_supervisor_startup.py::test_supervisor_later_popen_refusal_reaps_started_child_and_both_logs",
            "tests/test_release_1_0_supervisor_startup.py::test_supervisor_normal_stop_reaps_child_closes_log_and_restores_signals",
        ),
        ("cli:up",),
    ),
    "R10-CLI-RETAG-DOCUMENTS-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_report_matches_owned_changes_and_repeat_preserves_versions[show]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_report_matches_owned_changes_and_repeat_preserves_versions[apply]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_scripted_arbiter_requires_a_grounded_known_kind[valid]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_scripted_arbiter_requires_a_grounded_known_kind[unquoted]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_scripted_arbiter_requires_a_grounded_known_kind[new-kind]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_scripted_arbiter_requires_a_grounded_known_kind[error]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_limit_bounds_tag_writes_and_report_rows",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_disabled_arbiter_refuses_without_report_or_mutation",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_learns_owned_boilerplate_and_rebuilds_tags_without_foreign_writes[show]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_learns_owned_boilerplate_and_rebuilds_tags_without_foreign_writes[apply]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_invalid_report_target_is_safe_and_leaves_archive_unchanged[symlink]",
            "tests/test_release_1_0_cli_retag_paths.py::test_retag_cli_invalid_report_target_is_safe_and_leaves_archive_unchanged[directory]",
        ),
        ("cli:retag-documents",),
    ),
    "R10-CLI-IMPORT-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_review_gate_author_provenance_repeat_and_foreign_preservation[None]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_review_gate_author_provenance_repeat_and_foreign_preservation[import-purge-foreign]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_planning_and_missing_path_never_open_storage[dry]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_planning_and_missing_path_never_open_storage[missing]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_refuses_ambiguous_owner_or_unknown_author[ambiguous-owner]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_refuses_ambiguous_owner_or_unknown_author[unknown-author]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_limit_resumes_after_existing_prefix[2]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_limit_resumes_after_existing_prefix[20]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_import_cli_one_read_failure_reports_nonzero_and_keeps_other_files",
        ),
        ("cli:import",),
    ),
    "R10-CLI-PURGE-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_removes_owned_file_vault_versions_and_audits_private_receipt[False]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_removes_owned_file_vault_versions_and_audits_private_receipt[True]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_refusals_preserve_archive[no-confirm]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_refusals_preserve_archive[active]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_refusals_preserve_archive[wrong-owner]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_refusals_preserve_archive[missing]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_keeps_shared_file_until_last_reference",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_batch_limit_retention_and_partial_commit_receipts[False]",
            "tests/test_release_1_0_cli_import_purge_paths.py::test_purge_cli_batch_limit_retention_and_partial_commit_receipts[True]",
        ),
        ("cli:purge",),
    ),
    "R10-CLI-EXTRACT-STRUCTURE-RELATIONS-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_real_queue_show_apply_repeat_and_foreign_preservation[False]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_real_queue_show_apply_repeat_and_foreign_preservation[True]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_rejects_ungrounded_output_and_reports_model_failure[bad-quote]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_rejects_ungrounded_output_and_reports_model_failure[bad-type]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_rejects_ungrounded_output_and_reports_model_failure[malformed]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_rejects_ungrounded_output_and_reports_model_failure[error]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_limit_bounds_actual_document_and_model_work[1]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_structure_cli_limit_bounds_actual_document_and_model_work[50]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_model_graph_cli_real_disabled_router_refuses_without_mutation[extract-structure-relations]",
        ),
        ("cli:extract-structure-relations",),
    ),
    "R10-CLI-REVIEW-RELATION-CANDIDATES-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_model_graph_paths.py::test_model_graph_cli_real_disabled_router_refuses_without_mutation[review-relation-candidates]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[False-confirm]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[False-reject]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[False-unsure]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[False-disagree]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[False-malformed]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[True-confirm]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[True-reject]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[True-unsure]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[True-disagree]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation[True-malformed]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_limit_processes_only_one_owned_candidate",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_false_quote_rejects_without_calling_model",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_model_failure_is_truthful_without_exception_secret_in_report",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_invalid_report_target_refuses_without_graph_mutation[symlink]",
            "tests/test_release_1_0_cli_model_graph_paths.py::test_review_cli_invalid_report_target_refuses_without_graph_mutation[directory]",
        ),
        ("cli:review-relation-candidates",),
    ),
    "R10-CLI-EVAL-BOOTSTRAP-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_filter_show_save_provenance_repeat_and_foreign_preservation[False-json]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_filter_show_save_provenance_repeat_and_foreign_preservation[False-plain]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_filter_show_save_provenance_repeat_and_foreign_preservation[True-json]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_filter_show_save_provenance_repeat_and_foreign_preservation[True-plain]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_refuses_unusable_proposals_without_saving[paraphrase]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_refuses_unusable_proposals_without_saving[short]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_refuses_unusable_proposals_without_saving[empty]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_disabled_ambiguous_and_empty_refuse_or_report_without_mutation[disabled]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_disabled_ambiguous_and_empty_refuse_or_report_without_mutation[ambiguous]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_disabled_ambiguous_and_empty_refuse_or_report_without_mutation[empty]",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_limit_caps_real_model_proposals_and_saved_cases",
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py::test_eval_cli_one_model_failure_continues_and_never_prints_secret",
        ),
        ("cli:eval-bootstrap",),
    ),
    "R10-CLI-ENGINEER-COMMAND-STORE-PROVISION-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_provisions_authenticated_private_ledger_and_repeat_preserves_authority",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_invalid_key_refuses_without_creating_ledger[missing]",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_invalid_key_refuses_without_creating_ledger[short]",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_invalid_key_refuses_without_creating_ledger[public-mode]",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_invalid_key_refuses_without_creating_ledger[symlink]",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_store_symlink_refuses_without_writing_target",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_changed_master_cannot_rebind_existing_authority",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_live_leases_prevent_second_writer[backend]",
            "tests/test_release_1_0_cli_command_store_paths.py::test_command_store_cli_live_leases_prevent_second_writer[ledger]",
        ),
        ("cli:engineer-command-store-provision",),
    ),
    "R10-CLI-DATA-SOURCE-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_data_source_paths.py::test_real_cli_covers_all_five_source_actions_and_owner_boundaries",
            "tests/test_release_1_0_cli_data_source_paths.py::test_real_cli_refusals_have_exact_codes_no_touch_or_secret_persistence",
        ),
        ("cli:data-source",),
    ),
    "R10-CLI-INIT-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_setup_paths.py::test_cli_init_creates_private_configuration_without_account_access[False]",
            "tests/test_release_1_0_cli_setup_paths.py::test_cli_init_creates_private_configuration_without_account_access[True]",
            "tests/test_release_1_0_cli_setup_paths.py::test_cli_init_requires_force_and_rotates_only_the_requested_configuration",
            "tests/test_release_1_0_cli_setup_paths.py::test_cli_init_refuses_symlink_even_with_force_without_replacing_target",
        ),
        ("cli:init",),
    ),
    "R10-CLI-INSTALL-SERVICES-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_setup_paths.py::test_cli_install_services_writes_exact_two_units_without_account_access_or_activation[False]",
            "tests/test_release_1_0_cli_setup_paths.py::test_cli_install_services_writes_exact_two_units_without_account_access_or_activation[True]",
        ),
        ("cli:install-services",),
    ),
    "R10-CLI-SERIES-CONFLICTS-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_series_conflicts.py::test_series_cli_changes_only_selected_suggested_neighbour_and_reports_truthfully[show]",
            "tests/test_release_1_0_cli_series_conflicts.py::test_series_cli_changes_only_selected_suggested_neighbour_and_reports_truthfully[apply]",
            "tests/test_release_1_0_cli_series_conflicts.py::test_series_cli_repeated_apply_preserves_decisions_and_reports_remaining_queue",
            "tests/test_release_1_0_cli_series_conflicts.py::test_series_cli_refuses_active_role_without_opening_storage_or_changing_data[account-deletion]",
            "tests/test_release_1_0_cli_series_conflicts.py::test_series_cli_refuses_active_role_without_opening_storage_or_changing_data[backend]",
        ),
        ("cli:dismiss-series-conflicts",),
    ),
    "R10-CLI-REINDEX-EMBEDDINGS-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_reindex_preserves_object_and_chunk_vectors_and_marks_exact_tenants[owner]",
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_reindex_preserves_object_and_chunk_vectors_and_marks_exact_tenants[all]",
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_reindex_without_confirmation_opens_no_storage_and_changes_nothing",
        ),
        ("cli:reindex-embeddings",),
    ),
    "R10-CLI-DOCUMENT-DATES-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_document_dates_crosses_undated_pages_preserves_owner_and_repeats_safely",
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_document_dates_limit_bounds_actual_writes_inside_a_larger_batch",
        ),
        ("cli:backfill-document-dates",),
    ),
    "R10-CLI-RELATION-DATES-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_relation_dates_preserves_foreign_and_existing_dates_and_batches_history[show]",
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_relation_dates_preserves_foreign_and_existing_dates_and_batches_history[apply]",
            "tests/test_release_1_0_cli_archive_maintenance.py::test_cli_relation_dates_injected_second_write_failure_rolls_back_rows_history_and_event",
        ),
        ("cli:backfill-relation-dates",),
    ),
    "R10-CLI-ENTITY-BACKFILL-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_graph_maintenance.py::test_entity_cli_keeps_human_rejection_and_foreign_rows_and_reports_actual_changes[show]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_entity_cli_keeps_human_rejection_and_foreign_rows_and_reports_actual_changes[apply]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_entity_cli_rejects_non_declaring_method_before_opening_storage",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_entity_cli_limit_bounds_actual_writes_in_a_larger_batch",
        ),
        ("cli:backfill-entities",),
    ),
    "R10-CLI-ENTITY-PRUNE-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_graph_maintenance.py::test_prune_cli_only_tombstones_stale_unreviewed_owner_node[show]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_prune_cli_only_tombstones_stale_unreviewed_owner_node[apply]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_prune_cli_refuses_incomplete_corpus_without_any_write",
        ),
        ("cli:prune-entities",),
    ),
    "R10-CLI-RELATION-BACKFILL-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_graph_maintenance.py::test_relations_cli_only_proposes_owner_accepted_links_and_preserves_facts[show]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_relations_cli_only_proposes_owner_accepted_links_and_preserves_facts[apply]",
        ),
        ("cli:backfill-relations",),
    ),
    "R10-CLI-EXACT-DUPLICATES-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_graph_maintenance.py::test_exact_duplicates_cli_keeps_global_components_inside_each_owner[show]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_exact_duplicates_cli_keeps_global_components_inside_each_owner[apply]",
            "tests/test_release_1_0_cli_graph_maintenance.py::test_exact_duplicates_cli_failed_second_loser_rolls_back_whole_component",
        ),
        ("cli:resolve-exact-duplicates",),
    ),
    "R10-OBSIDIAN-ONBOARDING-OWNER-FORWARDING-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[GET-/api/obsidian/status-status-obsidian.read]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[GET-/api/obsidian/diagnostics-diagnostics-obsidian.read]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[POST-/api/obsidian/onboarding/start-start-obsidian.connect]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[GET-/api/obsidian/onboarding-onboarding-obsidian.read]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[POST-/api/obsidian/onboarding/check-check-obsidian.connect]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[POST-/api/obsidian/onboarding/confirm-open-confirm_open-obsidian.connect]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[POST-/api/obsidian/onboarding/retry-retry-obsidian.connect]",
            "tests/test_obsidian_router.py::test_onboarding_routes_use_the_shared_actors_own_id[POST-/api/obsidian/onboarding/cancel-cancel-obsidian.connect]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_empty_body_connect_post_refuses_nonempty_json_before_authority[start]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_empty_body_connect_post_refuses_nonempty_json_before_authority[check]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_empty_body_connect_post_refuses_nonempty_json_before_authority[confirm-open]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_empty_body_connect_post_refuses_nonempty_json_before_authority[retry]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_empty_body_connect_post_refuses_nonempty_json_before_authority[cancel]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[status-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[status-owner]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[diagnostics-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[diagnostics-owner]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[onboarding-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[onboarding-owner]",
        ),
        (
            "api:GET /api/obsidian/diagnostics",
            "api:GET /api/obsidian/onboarding",
            "api:GET /api/obsidian/status",
            "api:POST /api/obsidian/onboarding/cancel",
            "api:POST /api/obsidian/onboarding/check",
            "api:POST /api/obsidian/onboarding/confirm-open",
            "api:POST /api/obsidian/onboarding/retry",
            "api:POST /api/obsidian/onboarding/start",
        ),
    ),
    "R10-OBSIDIAN-SELECT-DEVICE-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_foreign_opaque_candidate_is_forwarded_only_under_the_authenticated_owner",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_malformed_candidate_id_is_refused_before_authority_or_runtime[empty]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_malformed_candidate_id_is_refused_before_authority_or_runtime[slash]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_malformed_candidate_id_is_refused_before_authority_or_runtime[space]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_malformed_candidate_id_is_refused_before_authority_or_runtime[length-97]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_malformed_candidate_id_is_refused_before_authority_or_runtime[non-string]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_unknown_well_formed_candidate_has_a_stable_404_after_owner_binding",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_candidate_id_exact_regex_upper_bound_is_forwarded_under_the_owner",
        ),
        ("api:POST /api/obsidian/onboarding/select-device",),
    ),
    "R10-OBSIDIAN-PUBLIC-SETUP-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_public_setup_reads_fragment_in_external_script_and_never_echoes_the_token",
            "tests/test_obsidian_router.py::test_public_setup_body_and_token_are_strictly_bounded",
            "tests/test_obsidian_server_integration.py::test_public_setup_resolver_has_an_independent_per_ip_rate_limit",
        ),
        (
            "api:GET /obsidian/setup",
            "api:GET /obsidian/setup.js",
            "api:POST /api/public/obsidian/setup/resolve",
        ),
    ),
    "R10-OBSIDIAN-PUBLIC-OPEN-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_public_open_launcher_uses_only_a_fragment_and_accepts_a_safe_exact_note_path",
        ),
        (
            "api:GET /obsidian/open",
            "api:GET /obsidian/open.js",
        ),
    ),
    "R10-OBSIDIAN-VAULT-ALIAS-HTTP": (
        "deterministic",
        ("tests/test_obsidian_router.py::test_vault_alias_is_owner_scoped_and_has_an_exact_body",),
        ("api:POST /api/obsidian/onboarding/vault-alias",),
    ),
    "R10-OBSIDIAN-VAULT-LIST-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_vaults_use_the_actor_owner_and_explicit_owner_inputs_are_rejected",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[vaults-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[vaults-owner]",
        ),
        ("api:GET /api/obsidian/vaults",),
    ),
    "R10-OBSIDIAN-NOTES-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_note_reads_and_search_are_owner_scoped_and_bounded",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[list-field]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-missing-q]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-duplicate-q]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-empty-q]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-q-length-1001]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-extra-field]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-limit-type]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-limit-zero]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-limit-101]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[search-duplicate-limit]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[read-missing-path]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[read-duplicate-path]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[read-extra-field]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[read-empty-path]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_grammar_refuses_every_outside_value_before_owner_binding[read-path-length-2049]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_note_query_exact_upper_and_lower_bounds_are_forwarded_under_the_owner",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[notes-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[notes-owner]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[search-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[search-owner]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[read-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[read-owner]",
        ),
        (
            "api:GET /api/obsidian/notes",
            "api:GET /api/obsidian/notes/read",
            "api:GET /api/obsidian/notes/search",
        ),
    ),
    "R10-OBSIDIAN-OPERATIONS-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_router.py::test_operation_routes_never_accept_an_explicit_owner",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[operation-user]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_get_refuses_each_explicit_owner_key_before_authority[operation-owner]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_operation_id_outside_bounds_is_refused_without_runtime[empty]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_operation_id_outside_bounds_is_refused_without_runtime[length-201]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_operation_id_outside_bounds_is_refused_without_runtime[nul]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_operation_id_exact_upper_bound_is_forwarded_under_the_owner",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_operation_body_exact_byte_limit_passes_and_next_byte_is_refused",
        ),
        (
            "api:POST /api/obsidian/operations",
            "api:GET /api/obsidian/operations/{operation_id}",
        ),
    ),
    "R10-OBSIDIAN-REGISTRATION-AUTH-HEALTH-HTTP": (
        "deterministic",
        (
            "tests/test_obsidian_server_integration.py::test_optional_organ_and_routes_exist_only_when_enabled",
            "tests/test_obsidian_server_integration.py::test_public_health_attests_only_obsidian_mode_and_effective_root_digest",
            "tests/test_obsidian_server_integration.py::test_enabled_server_registers_tools_and_only_setup_is_public",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_server_has_exactly_all_21_obsidian_routes_enabled_and_none_disabled",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[status]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[diagnostics]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[start]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[onboarding]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[check]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[select-device]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[confirm-open]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[retry]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[cancel]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[vault-alias]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[vaults]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[notes]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[search]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[read]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[execute-operation]",
            "tests/test_release_1_0_obsidian_http_boundaries.py::test_every_owner_http_route_refuses_an_unauthenticated_request[get-operation]",
        ),
        (
            "api:GET /api/obsidian/diagnostics",
            "api:GET /api/obsidian/notes",
            "api:GET /api/obsidian/notes/read",
            "api:GET /api/obsidian/notes/search",
            "api:GET /api/obsidian/onboarding",
            "api:GET /api/obsidian/operations/{operation_id}",
            "api:GET /api/obsidian/status",
            "api:GET /api/obsidian/vaults",
            "api:GET /obsidian/open",
            "api:GET /obsidian/open.js",
            "api:GET /obsidian/setup",
            "api:GET /obsidian/setup.js",
            "api:POST /api/obsidian/onboarding/cancel",
            "api:POST /api/obsidian/onboarding/check",
            "api:POST /api/obsidian/onboarding/confirm-open",
            "api:POST /api/obsidian/onboarding/retry",
            "api:POST /api/obsidian/onboarding/select-device",
            "api:POST /api/obsidian/onboarding/start",
            "api:POST /api/obsidian/onboarding/vault-alias",
            "api:POST /api/obsidian/operations",
            "api:POST /api/public/obsidian/setup/resolve",
            "api:GET /api/health",
        ),
    ),
    "R10-CLI-BACKUP-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_backup_creates_verified_private_database_pair_and_truthful_scope[None-False]",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_backup_creates_verified_private_database_pair_and_truthful_scope[cli-own-fixture-False]",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_backup_creates_verified_private_database_pair_and_truthful_scope[cli-mirror-fixture-True]",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_backup_export_refuse_held_deletion_lease_before_real_handlers[backup]",
        ),
        ("cli:backup",),
    ),
    "R10-CLI-VERIFY-BACKUP-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_verify_explicit_and_latest_backup_then_detects_corrupted_manifest",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_verify_empty_store_and_invalid_path_are_visible_refusals",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_backup_export_refuse_held_deletion_lease_before_real_handlers[verify-backup]",
        ),
        ("cli:verify-backup",),
    ),
    "R10-CLI-EXPORT-USER-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_export_writes_one_private_tenant_artifact_with_selected_rows",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_export_unknown_user_creates_no_artifact",
            "tests/test_release_1_0_cli_backup_export_paths.py::test_cli_backup_export_refuse_held_deletion_lease_before_real_handlers[export-user]",
        ),
        ("cli:export-user",),
    ),
    "R10-CLI-BACKUP-KEYGEN-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_keygen_creates_private_key_without_account_access_or_secret_output[False]",
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_keygen_creates_private_key_without_account_access_or_secret_output[True]",
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_keygen_requires_target_and_explicit_force_to_replace_existing_key",
        ),
        ("cli:backup-keygen",),
    ),
    "R10-CLI-DECRYPT-BACKUP-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_decrypt_restores_exact_bytes_with_explicit_or_default_paths[False]",
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_decrypt_restores_exact_bytes_with_explicit_or_default_paths[True]",
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_decrypt_missing_key_configuration_is_visible_and_preserves_fixture",
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_decrypt_existing_destination_is_never_overwritten",
            "tests/test_release_1_0_cli_crypto_paths.py::test_cli_decrypt_corrupt_ciphertext_does_not_publish_a_failed_destination",
        ),
        ("cli:decrypt-backup",),
    ),
    "R10-CLI-RESTORE-BACKUP-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_selects_explicit_or_latest_distinct_snapshot_and_keeps_safety_copy[False]",
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_selects_explicit_or_latest_distinct_snapshot_and_keeps_safety_copy[True]",
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_without_yes_refuses_a_valid_target_before_storage_restore",
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_refuses_each_active_role_before_mutating_restore_stage[account-deletion]",
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_refuses_each_active_role_before_mutating_restore_stage[backend]",
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_refuses_each_active_role_before_mutating_restore_stage[telegram-bridge]",
            "tests/test_release_1_0_cli_restore_paths.py::test_cli_restore_rejects_corrupt_manifest_and_keeps_current_rows_and_archive",
        ),
        ("cli:restore-backup",),
    ),
    "R10-CLI-STATUS-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_read_paths.py::test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit[ready-0-status]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit[attention-0-status]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit[error-1-status]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_status_human_output_and_default_flags",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_account_deletion_lease_blocks_each_read_command_without_rows_changed[status]",
        ),
        ("cli:status",),
    ),
    "R10-CLI-DOCTOR-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_read_paths.py::test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit[ready-0-doctor]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit[attention-0-doctor]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit[error-1-doctor]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_account_deletion_lease_blocks_each_read_command_without_rows_changed[doctor]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_invalid_argv_exits_before_handler_and_state_access[arguments0]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_doctor_default_flags_and_exact_ready_json",
        ),
        ("cli:doctor",),
    ),
    "R10-CLI-EVENTS-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_read_paths.py::test_cli_account_deletion_lease_blocks_each_read_command_without_rows_changed[events]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_events_real_storage_has_exact_default_page_filter_and_empty_text",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_events_empty_store_has_literal_human_and_json_results",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_invalid_argv_exits_before_handler_and_state_access[arguments2]",
        ),
        ("cli:events",),
    ),
    "R10-CLI-SEARCH-SOURCE-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_read_paths.py::test_cli_account_deletion_lease_blocks_each_read_command_without_rows_changed[search-source]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_search_source_preserves_verdict_tenant_and_private_projection",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_search_source_empty_result_keeps_literal_explanation",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_invalid_argv_exits_before_handler_and_state_access[arguments1]",
        ),
        ("cli:search-source",),
    ),
    "R10-CLI-MODEL-CHECK-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_read_paths.py::test_cli_model_check_json_forwards_timeout_and_needs_no_account_lease[True-0]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_model_check_json_forwards_timeout_and_needs_no_account_lease[False-1]",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_model_check_disabled_human_mode_still_calls_provider_boundary",
            "tests/test_release_1_0_cli_read_paths.py::test_cli_invalid_argv_exits_before_handler_and_state_access[arguments3]",
        ),
        ("cli:model-check",),
    ),
    "R10-CLI-MINT-TOKEN-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_pins_output_hash_expiry_target_preset_and_foreign_preservation[False-None-None-user]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_pins_output_hash_expiry_target_preset_and_foreign_preservation[False-moderator-30m-moderator]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_pins_output_hash_expiry_target_preset_and_foreign_preservation[True-None-1h-admin]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_pins_output_hash_expiry_target_preset_and_foreign_preservation[True-guest-None-guest]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_invalid_inputs_leave_selected_tables_and_no_secret[arguments0-\\u041d\\u0435\\u0438\\u0437\\u0432\\u0435\\u0441\\u0442\\u043d\\u044b\\u0439 preset: bogus.]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_invalid_inputs_leave_selected_tables_and_no_secret[arguments1-\\u041d\\u0435\\u043a\\u043e\\u0440\\u0440\\u0435\\u043a\\u0442\\u043d\\u044b\\u0439 --ttl: 'soon'.]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_mint_invalid_inputs_leave_selected_tables_and_no_secret[arguments2-user_id must be 1-200 characters]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_token_commands_refuse_both_held_leases_without_rows_changed[account-deletion-mint-token]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_token_commands_refuse_both_held_leases_without_rows_changed[backend-mint-token]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_token_missing_required_argument_exits_before_storage[mint-token]",
        ),
        ("cli:mint-token",),
    ),
    "R10-CLI-REVOKE-TOKEN-ARGV": (
        "deterministic",
        (
            "tests/test_release_1_0_cli_token_paths.py::test_cli_revoke_changes_only_target_timestamp_and_replay_missing_are_exact_refusals",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_token_commands_refuse_both_held_leases_without_rows_changed[account-deletion-revoke-token]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_token_commands_refuse_both_held_leases_without_rows_changed[backend-revoke-token]",
            "tests/test_release_1_0_cli_token_paths.py::test_cli_token_missing_required_argument_exits_before_storage[revoke-token]",
        ),
        ("cli:revoke-token",),
    ),
    "R10-ADMIN-ENTITY-LIST-HTTP": (
        "deterministic",
        (
            "tests/test_admin_entity_list_http.py::test_admin_entities_lists_filtered_paginated_target_without_foreign_rows",
            "tests/test_admin_entity_list_http.py::test_admin_entities_invalid_filters_do_not_read_or_audit",
        ),
        ("api:GET /api/admin/entities",),
    ),
    "R10-KG-LINK-HTTP": (
        "deterministic",
        (
            "tests/test_kg_link_http.py::test_kg_link_creates_then_upserts_exact_public_raw_and_audit",
            "tests/test_kg_link_http.py::test_kg_link_invalid_and_foreign_inputs_preserve_selected_rows",
            "tests/test_kg_link_http.py::test_kg_link_auth_and_loopback_csrf_refusals_have_exact_audit_effects",
        ),
        ("api:POST /api/kg/link",),
    ),
    "R10-KG-RESOLUTION-DETECT-HTTP": (
        "deterministic",
        (
            "tests/test_kg_resolution_detect_http.py::test_kg_resolution_detect_shared_alias_has_one_exact_candidate",
            "tests/test_kg_resolution_detect_http.py::test_kg_resolution_detect_compact_identifiers_have_no_candidate",
        ),
        ("api:POST /api/kg/resolutions/detect",),
    ),
    "R10-KG-RESOLUTION-REJECT-HTTP": (
        "deterministic",
        (
            "tests/test_kg_resolution_reject_http.py::test_kg_reject_moderator_and_admin_change_only_decision_and_audit_replay",
            "tests/test_kg_resolution_reject_http.py::test_kg_reject_missing_foreign_and_merged_are_404_without_selected_effects",
            "tests/test_kg_resolution_reject_http.py::test_kg_reject_user_and_guest_are_403_without_selected_effects",
        ),
        ("api:POST /api/kg/resolutions/{candidate_id}/reject",),
    ),
    "R10-ADMIN-ENTITY-SUGGESTION-GROUP-DECIDE-HTTP": (
        "deterministic",
        (
            "tests/test_the_suggestion_queue_exists.py::test_group_accept_is_one_decision_for_every_document",
            "tests/test_the_suggestion_queue_exists.py::test_group_reject_records_refusal_without_creating_a_node",
        ),
        ("api:POST /api/admin/entity-suggestions/groups/decide",),
    ),
    "R10-ADMIN-KNOWLEDGE-DETECT-DUPLICATES-HTTP": (
        "deterministic",
        (
            "tests/test_knowledge_dedup.py::test_admin_knowledge_detect_duplicates_persists_one_exact_pair",
            "tests/test_knowledge_dedup.py::test_admin_knowledge_detect_duplicates_orthogonal_pair_stays_separate",
        ),
        ("api:POST /api/admin/knowledge/detect-duplicates",),
    ),
    "R10-ADMIN-KNOWLEDGE-ENTITY-CONFIRM-HTTP": (
        "deterministic",
        (
            "tests/test_confirming_an_entity_closes_the_chain.py::test_confirming_a_candidate_creates_the_node_and_an_accepted_link",
            "tests/test_confirming_an_entity_closes_the_chain.py::test_an_unknown_entity_type_is_refused_by_name",
        ),
        ("api:POST /api/admin/knowledge/{knowledge_id}/entities",),
    ),
    "R10-ADMIN-RESOLUTION-DETECT-HTTP": (
        "deterministic",
        (
            "tests/test_entity_dedup_sweeps.py::test_admin_resolution_detect_shared_alias_has_one_exact_candidate",
            "tests/test_entity_dedup_sweeps.py::test_admin_resolution_detect_compact_identifiers_have_no_candidate",
        ),
        ("api:POST /api/admin/resolutions/detect",),
    ),
    "R10-ADMIN-RESOLUTION-REJECT-HTTP": (
        "deterministic",
        (
            "tests/test_admin_links_reject_http.py::test_admin_reject_changes_only_candidate_decision_and_replays",
            "tests/test_admin_links_reject_http.py::test_admin_reject_missing_and_foreign_are_404_without_selected_effects[missing]",
            "tests/test_admin_links_reject_http.py::test_admin_reject_missing_and_foreign_are_404_without_selected_effects[foreign]",
        ),
        ("api:POST /api/admin/resolutions/{candidate_id}/reject",),
    ),
    "R10-ADMIN-KNOWLEDGE-ENTITY-LINK-HTTP": (
        "deterministic",
        (
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_create_and_upsert_pin_public_storage_and_audit",
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_refusals_are_400_without_selected_effects[missing-knowledge]",
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_refusals_are_400_without_selected_effects[missing-entity]",
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_refusals_are_400_without_selected_effects[foreign-knowledge]",
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_refusals_are_400_without_selected_effects[foreign-entity]",
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_refusals_are_400_without_selected_effects[invalid-status]",
            "tests/test_admin_links_reject_http.py::test_admin_entity_link_refusals_are_400_without_selected_effects[invalid-confidence]",
        ),
        ("api:POST /api/admin/knowledge/{knowledge_id}/entity-links",),
    ),
    "R10-GRAPH-PATH-HTTP": (
        "deterministic",
        (
            "tests/test_graph_path_http.py::test_graph_path_ids_pin_steps_depth_and_preserve_rows[forward]",
            "tests/test_graph_path_http.py::test_graph_path_ids_pin_steps_depth_and_preserve_rows[reverse]",
            "tests/test_graph_path_http.py::test_graph_path_ids_pin_steps_depth_and_preserve_rows[depth-one]",
            "tests/test_graph_path_http.py::test_graph_path_ids_pin_steps_depth_and_preserve_rows[disconnected]",
            "tests/test_graph_path_http.py::test_graph_path_ids_pin_steps_depth_and_preserve_rows[default-depth]",
            "tests/test_graph_path_http.py::test_graph_path_names_and_aliases_resolve_only_own_entities[source-name]",
            "tests/test_graph_path_http.py::test_graph_path_names_and_aliases_resolve_only_own_entities[target-name]",
            "tests/test_graph_path_http.py::test_graph_path_names_and_aliases_resolve_only_own_entities[source-alias]",
            "tests/test_graph_path_http.py::test_graph_path_names_and_aliases_resolve_only_own_entities[target-alias]",
            "tests/test_graph_path_http.py::test_graph_path_missing_and_foreign_endpoints_are_404[missing-source]",
            "tests/test_graph_path_http.py::test_graph_path_missing_and_foreign_endpoints_are_404[missing-target]",
            "tests/test_graph_path_http.py::test_graph_path_missing_and_foreign_endpoints_are_404[foreign-source]",
            "tests/test_graph_path_http.py::test_graph_path_missing_and_foreign_endpoints_are_404[foreign-target]",
            "tests/test_graph_path_http.py::test_graph_path_validates_depth_and_authority[depth-low]",
            "tests/test_graph_path_http.py::test_graph_path_validates_depth_and_authority[depth-high]",
            "tests/test_graph_path_http.py::test_graph_path_validates_depth_and_authority[anonymous]",
            "tests/test_graph_path_http.py::test_graph_path_validates_depth_and_authority[kg-read-denied]",
        ),
        ("api:GET /api/kg/graph-path",),
    ),
    "R10-ADMIN-CONTAINER-CREATE-HTTP": (
        "deterministic",
        ("tests/test_containers_browse.py::test_admin_tags_containers_and_entity_filter",),
        ("api:POST /api/admin/containers",),
    ),
    "R10-ADMIN-EPISODE-BASELINE-HTTP": (
        "deterministic",
        (
            "tests/test_interaction_episode_baseline_api.py::test_episode_baseline_endpoint_is_bounded_body_free_and_cross_tenant_audited",
        ),
        ("api:GET /api/admin/eval/interaction-episode-baseline",),
    ),
    "R10-ADMIN-RELATION-REVIEW-INVALIDATE-HTTP": (
        "deterministic",
        (
            "tests/test_api_vertical_slice.py::test_maturity_workflows_are_reachable_through_signed_and_admin_apis",
        ),
        (
            "api:GET /api/admin/relation-candidates",
            "api:POST /api/admin/relation-candidates/{candidate_id}/review",
            "api:POST /api/admin/relations/{relation_id}/invalidate",
        ),
    ),
    "R10-PRODUCTION-OBSERVATION-HTTP": (
        "deterministic",
        (
            "tests/test_production_read_only_observation_route.py::test_real_lifespan_collector_uses_the_existing_storage_connection",
        ),
        ("api:GET /api/admin/production-read-only-observation",),
    ),
    "R10-OBSERVER-SNAPSHOT-HTTP": (
        "deterministic",
        (
            "tests/test_document_contour_observer_snapshot.py::test_http_snapshot_is_owner_only_and_numeric_loopback_only",
        ),
        ("api:GET /api/admin/document-contour-observer-snapshot",),
    ),
    "R10-FEEDBACK-WRITE-HTTP": (
        "deterministic",
        ("tests/test_maturity_070.py::test_feedback_state_replaces_rating_and_updates_usage_attribution",),
        ("api:POST /api/feedback",),
    ),
    "R10-ADMIN-FEEDBACK-HTTP": (
        "deterministic",
        ("tests/test_maturity_070.py::test_current_feedback_stats_replace_superseded_signal",),
        ("api:GET /api/admin/feedback",),
    ),
    "R10-ADMIN-ENTITY-WRITES-HTTP": (
        "deterministic",
        (
            "tests/test_admin_entity_mutation_http.py::test_admin_entity_create_persists_exact_target_row_and_first_version",
            "tests/test_admin_entity_mutation_http.py::test_admin_entity_patch_preserves_foreign_rows_and_appends_exact_revision",
            "tests/test_admin_entity_mutation_http.py::test_admin_entity_delete_records_tombstone_once_and_preserves_other_tenant",
        ),
        (
            "api:POST /api/admin/entities",
            "api:PATCH /api/admin/entities/{entity_id}",
            "api:DELETE /api/admin/entities/{entity_id}",
        ),
    ),
    "R10-CONFLICT-DECIDE-HTTP": (
        "deterministic",
        (
            "tests/test_conflict_triage_in_chat.py::test_http_conflict_decide_dismisses_and_hides_from_suggested",
        ),
        ("api:POST /api/kg/conflicts/{conflict_id}/decide",),
    ),
    "R10-ADMIN-LINK-REVIEW-HTTP": (
        "deterministic",
        (
            "tests/test_relations_actually_get_found.py::test_accepting_a_link_reconsiders_the_relations",
            "tests/test_relations_actually_get_found.py::test_rejecting_a_link_proposes_nothing",
        ),
        ("api:PATCH /api/admin/entity-links/{link_id}",),
    ),
    "R10-RESOLUTION-MERGE-HTTP": (
        "deterministic",
        (
            "tests/test_graph_runtime_log_privacy.py::test_resolution_and_merge_surfaces_never_publish_snapshots_or_evidence",
            "tests/test_resolution_queue_is_a_page.py::test_the_next_page_shows_what_the_first_one_hid",
            "tests/test_resolution_queue_is_a_page.py::test_the_queue_reports_the_whole_size_not_the_page_size",
        ),
        (
            "api:GET /api/kg/resolutions",
            "api:GET /api/kg/resolutions/pending",
            "api:GET /api/admin/resolutions",
            "api:POST /api/kg/resolutions/{candidate_id}/accept",
            "api:POST /api/admin/resolutions/{candidate_id}/accept",
            "api:GET /api/kg/merges",
            "api:GET /api/admin/merges",
            "api:POST /api/kg/merges/{merge_id}/undo",
            "api:POST /api/admin/merges/{merge_id}/undo",
        ),
    ),
    "R10-TEXT-IMPORT-HTTP": (
        "deterministic",
        (
            "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[assessed]",
            "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[unless_explicit]",
            "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[always]",
            "tests/test_organs_importer.py::test_import_endpoint_queues_reviews_and_is_idempotent",
            "tests/test_organs_importer.py::test_import_endpoint_rejects_unknown_format_and_requires_auth",
        ),
        ("api:POST /api/ingest", "api:POST /api/import"),
    ),
    "R10-CONFLICT-QUEUE-HTTP": (
        "deterministic",
        ("tests/test_conflict_queue_triage_hints.py::test_http_conflict_list_includes_triage_hint",),
        ("api:GET /api/kg/conflicts",),
    ),
    "R10-RELATION-CANDIDATE-HTTP": (
        "deterministic",
        (
            "tests/test_relation_triage_in_chat.py::test_api_list_and_review_are_tenant_scoped_capability_gated_and_content_free",
            "tests/test_relation_triage_in_chat.py::test_review_is_idempotent_but_terminal_and_audit_is_content_free",
            "tests/test_relation_triage_in_chat.py::test_review_identity_comes_from_actor_not_request_body",
            "tests/test_relation_triage_in_chat.py::test_write_only_grant_cannot_read_or_review_relation_cards",
        ),
        ("api:GET /api/kg/relation-candidates", "api:POST /api/kg/relation-candidates/{candidate_id}/review"),
    ),
    "R10-CLEANUP-HTTP": (
        "deterministic",
        ("tests/test_product_quality.py::test_admin_quality_workflows_and_api_ingest_default",),
        ("api:GET /api/admin/cleanup/legacy", "api:POST /api/admin/cleanup/legacy/apply"),
    ),
    "R10-APPROVAL-LIST-HTTP": (
        "deterministic",
        (
            "tests/test_approval_reaches_the_person.py::test_the_route_executes_on_approval_and_only_once",
            "tests/test_approval_reaches_the_person.py::test_a_rejection_does_not_execute",
            "tests/test_approval_reaches_the_person.py::test_an_approval_that_cannot_execute_says_so",
            "tests/test_approval_reaches_the_person.py::test_the_chat_command_lists_what_waits_and_names_unknown_outcomes",
            "tests/test_approval_reaches_the_person.py::test_a_bystander_pressing_the_button_changes_nothing",
            "tests/test_one_operation_through_every_surface.py::test_a_stranger_sees_nothing_on_any_surface",
        ),
        ("api:GET /api/me/approvals",),
    ),
    "R10-APPROVAL-DECIDE-HTTP": (
        "deterministic",
        (
            "tests/test_approval_reaches_the_person.py::test_the_route_executes_on_approval_and_only_once",
            "tests/test_approval_reaches_the_person.py::test_a_rejection_does_not_execute",
            "tests/test_approval_reaches_the_person.py::test_an_approval_that_cannot_execute_says_so",
            "tests/test_approval_reaches_the_person.py::test_a_bystander_pressing_the_button_changes_nothing",
        ),
        ("api:POST /api/approvals/{approval_id}/decide",),
    ),
    "R10-BRIDGE-EVENTS-HTTP": (
        "deterministic",
        (
            "tests/test_bridge_events.py::test_a_bridge_event_lands_in_the_journal",
            "tests/test_bridge_events.py::test_an_unknown_event_type_is_refused",
            "tests/test_bridge_events.py::test_the_endpoint_requires_bridge_authentication",
            "tests/test_bridge_events.py::test_the_payload_is_bounded_on_both_axes",
            "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.dead_letter]",
            "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.outbound_failed]",
            "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.outbound_recovered]",
            "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.poll_failed]",
            "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.poll_recovered]",
        ),
        ("api:POST /api/events",),
    ),
    "R10-KNOWLEDGE-SOURCES-HTTP": (
        "deterministic",
        ("tests/test_source_search.py::test_source_search_over_http_excludes_rejected_material",),
        ("api:GET /api/knowledge/sources",),
    ),
    "R10-KNOWLEDGE-BY-DATE-HTTP": (
        "deterministic",
        ("tests/test_timeline_in_chat.py::test_the_period_total_comes_from_the_route_not_from_the_page",),
        ("api:GET /api/knowledge/by-date",),
    ),
    "R10-KNOWLEDGE-PERSONAL-ISOLATION-HTTP": (
        "deterministic",
        ("tests/test_shared_archive.py::test_without_the_setting_isolation_is_intact",),
        (
            "api:GET /api/knowledge",
            "api:GET /api/knowledge/tags",
            "api:GET /api/knowledge/sources",
            "api:GET /api/knowledge/by-date",
        ),
    ),
    "R10-MONITOR-HTTP": (
        "deterministic",
        (
            "tests/test_monitors_watch_a_topic.py::test_monitors_are_self_service_over_http",
            "tests/test_monitors_watch_a_topic.py::test_a_foreign_monitor_cannot_be_stopped",
            "tests/test_monitors_watch_a_topic.py::test_a_person_cannot_hoard_monitors",
        ),
        (
            "api:GET /api/me/monitors",
            "api:POST /api/me/monitors",
            "api:POST /api/me/monitors/{monitor_id}/stop",
        ),
    ),
    "R10-REFLECTION-HTTP": (
        "deterministic",
        (
            "tests/test_organs_reflection.py::test_reflection_endpoint_returns_digest_for_actor",
            "tests/test_organs_reflection.py::test_build_reflection_summarises_state",
        ),
        ("api:GET /api/reflection",),
    ),
    "R10-NOTIFICATION-HTTP": (
        "deterministic",
        (
            "tests/test_engineer_terminal_notification_api.py::test_pending_projects_no_raw_handle_and_artifact_is_exact",
            "tests/test_engineer_terminal_notification_api.py::test_revocation_after_stage_retires_without_exposing_bytes[identity]",
            "tests/test_engineer_terminal_notification_api.py::test_revocation_after_stage_retires_without_exposing_bytes[capability]",
            "tests/test_engineer_terminal_notification_api.py::test_revocation_after_stage_retires_without_exposing_bytes[account]",
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_pending_does_not_require_file_read",
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_claim_reauthorizes_after_pointer_listing[identity]",
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_claim_reauthorizes_after_pointer_listing[capability]",
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_claim_reauthorizes_after_pointer_listing[account]",
            "tests/test_organs_reminders.py::test_notification_endpoints_require_bridge_actor",
            "tests/test_notification_queue_head.py::test_a_de_allowlisted_chat_does_not_block_the_queue",
            "tests/test_approval_reaches_the_person.py::test_the_bridge_receives_what_it_needs_to_draw_the_buttons",
        ),
        (
            "api:GET /api/notifications/pending",
            "api:GET /api/notifications/{notification_id}/artifact",
            "api:POST /api/notifications/ack",
            "api:POST /api/notifications/{notification_id}/claim",
        ),
    ),
    "R10-CONTAINER-BROWSE-HTTP": (
        "deterministic",
        (
            "tests/test_containers_browse.py::test_http_tags_containers_and_filters",
            "tests/test_containers_browse.py::test_create_container_validates_kind_parent_and_builds_part_of",
            "tests/test_containers_browse.py::test_container_knowledge_count_reflects_accepted_members",
            "tests/test_containers_browse.py::test_list_knowledge_tags_counts_casefold_and_excludes_deleted",
        ),
        (
            "api:GET /api/knowledge",
            "api:GET /api/knowledge/tags",
            "api:GET /api/kg/entities",
            "api:GET /api/kg/containers",
            "api:POST /api/kg/containers",
            "api:GET /api/kg/stats",
        ),
    ),
    "R10-EVENT-TIMELINE-HTTP": (
        "deterministic",
        (
            "tests/test_event_timeline.py::test_timeline_and_set_time_over_http",
            "tests/test_event_timeline.py::test_unified_timeline_page_is_exposed_over_http",
            "tests/test_event_timeline.py::test_set_event_time_validates_type_dates_and_range",
            "tests/test_event_timeline.py::test_timeline_page_unifies_events_and_relation_changes_under_one_limit",
        ),
        ("api:GET /api/kg/timeline", "api:POST /api/kg/entities/{entity_id}/time"),
    ),
    "R10-COMPACTS-HTTP": (
        "deterministic",
        (
            "tests/test_the_compact_tab_has_something_to_show.py::test_the_list_is_empty_before_any_run",
            "tests/test_the_compact_tab_has_something_to_show.py::test_a_run_appears_in_the_list_and_reads_back",
            "tests/test_the_compact_tab_has_something_to_show.py::test_running_the_same_day_twice_makes_one_row",
            "tests/test_the_compact_tab_has_something_to_show.py::test_a_malformed_date_is_refused_not_guessed",
            "tests/test_the_compact_tab_has_something_to_show.py::test_someone_elses_compact_is_only_for_the_owner",
            "tests/test_the_compact_tab_has_something_to_show.py::test_the_list_carries_the_human_wording",
        ),
        (
            "api:GET /api/compacts",
            "api:POST /api/compacts/run",
        ),
    ),
    "R10-MISSIONS-HTTP": (
        "deterministic",
        ("tests/test_executive.py::test_mission_http_endpoints_create_list_and_stop",),
        (
            "api:POST /api/missions",
            "api:GET /api/missions",
            "api:GET /api/missions/{mission_id}",
            "api:POST /api/missions/{mission_id}/stop",
        ),
    ),
    "R10-ADMIN-MISSIONS-HTTP": (
        "deterministic",
        (
            "tests/test_executive.py::test_mission_http_endpoints_create_list_and_stop",
            "tests/test_mission_oversight_boundaries.py::test_reading_another_accounts_mission_is_recorded",
            "tests/test_mission_oversight_boundaries.py::test_a_delegated_admin_cannot_cancel_the_owners_mission",
            "tests/test_mission_oversight_boundaries.py::test_cancelling_an_ordinary_accounts_mission_still_works_and_is_recorded",
        ),
        (
            "api:GET /api/admin/missions",
            "api:GET /api/admin/missions/{mission_id}",
            "api:POST /api/admin/missions/{mission_id}/cancel",
        ),
    ),
    "R10-EXPORT-HTTP": (
        "deterministic",
        (
            "tests/test_the_export_says_what_it_is.py::test_body_free_response_does_not_advertise_a_nonexistent_plaintext_vault",
            "tests/test_the_export_says_what_it_is.py::test_explicit_full_owner_response_names_the_readable_vault",
            "tests/test_the_export_says_what_it_is.py::test_it_admits_what_it_leaves_behind",
            "tests/test_owner_mutation_boundaries.py::test_delegated_admin_cannot_export_owner_archive",
            "tests/test_export_private_reminder_isolation.py::test_export_keeps_dependencies_of_the_users_exact_private_alias",
            "tests/test_export_private_reminder_isolation.py::test_export_uses_current_and_historical_private_alias_identity_tokens",
            "tests/test_export_private_reminder_isolation.py::test_tenant_export_does_not_include_another_persons_monitor_query_or_chat",
        ),
        (
            "api:POST /api/admin/exports",
            "api:GET /api/admin/exports/{filename}/download",
        ),
    ),
    "R10-ADMIN-FILE-DOWNLOAD-HTTP": (
        "deterministic",
        (
            "tests/test_file_delivery_privacy.py::test_admin_download_revalidates_immediately_before_atomic_read",
        ),
        ("api:GET /api/admin/files/{raw_id}/download",),
    ),
    "R10-ENTITY-CRUD-HTTP": (
        "deterministic",
        (
            "tests/test_graph_runtime_log_privacy.py::test_entity_audit_retains_no_content_or_content_hash_after_hard_purge",
            "tests/test_entity_edits_are_reversible.py::test_http_restore_is_self_service_and_audited",
            "tests/test_entity_edits_are_reversible.py::test_a_deleted_object_can_be_brought_back",
            "tests/test_entity_edits_are_reversible.py::test_restoring_an_entity_version_is_a_new_version_not_a_rewind",
            "tests/test_entity_edits_are_reversible.py::test_restore_does_not_cross_tenants",
        ),
        (
            "api:POST /api/kg/entities",
            "api:PATCH /api/kg/entities/{entity_id}",
            "api:DELETE /api/kg/entities/{entity_id}",
            "api:POST /api/kg/entities/{entity_id}/restore",
            "api:POST /api/kg/entities/{entity_id}/undelete",
        ),
    ),
    "R10-KNOWLEDGE-CRUD-HTTP": (
        "deterministic",
        (
            "tests/test_the_audit_log_never_takes_a_document_body.py::test_the_own_edit_and_delete_routes_cannot_copy_a_note_into_audit",
            "tests/test_shared_archive.py::test_one_person_finds_and_edits_what_another_wrote",
            "tests/test_users_can_forget_their_own_knowledge.py::test_a_plain_user_can_delete_their_own_knowledge_object",
            "tests/test_users_can_forget_their_own_knowledge.py::test_a_plain_user_still_cannot_delete_someone_elses_knowledge_object",
        ),
        (
            "api:PATCH /api/knowledge/{knowledge_id}",
            "api:DELETE /api/knowledge/{knowledge_id}",
        ),
    ),
    "R10-EVAL-HTTP": (
        "deterministic",
        (
            "tests/test_eval_harness.py::test_eval_endpoints_end_to_end",
            "tests/test_eval_harness.py::test_metric_functions",
            "tests/test_eval_harness.py::test_add_list_delete_eval_case",
            "tests/test_eval_harness.py::test_run_eval_measures_and_detects_regression",
            "tests/test_eval_harness.py::test_run_eval_empty_gold_set",
            "tests/test_graph_policy_is_declared.py::test_admin_eval_search_declares_the_policy_and_writes_no_usage",
        ),
        (
            "api:GET /api/admin/eval/search",
            "api:GET /api/admin/eval/cases",
            "api:POST /api/admin/eval/cases",
            "api:DELETE /api/admin/eval/cases/{case_id}",
            "api:POST /api/admin/eval/run",
        ),
    ),
    "R10-RETRIEVAL-EXPLAIN-HTTP": (
        "deterministic",
        (
            "tests/test_retrieval_explain.py::test_retrieval_explain_endpoint",
            "tests/test_retrieval_explain.py::test_score_reconstructs_from_components",
            "tests/test_search_explain_contract.py::test_search_explain_api_is_privacy_safe_and_reports_unavailable_corpora",
            "tests/test_search_explain_contract.py::test_search_explain_api_rejects_unknown_corpus_and_date_role",
        ),
        ("api:GET /api/admin/retrieval/explain",),
    ),
    "R10-API-ROOT-REDIRECT-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_http_binds_redirect_swagger_and_selected_openapi_contracts",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[redirect_target]",
        ),
        ("api:GET /",),
    ),
    "R10-API-METADATA-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_http_binds_redirect_swagger_and_selected_openapi_contracts",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_http_refuses_anonymous_denied_and_revoked_without_content_or_writes",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[swagger_mount]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[swagger_url]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_operation]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_required]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_duplicate]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_limit]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_diagnostics_default]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_version]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[success_content]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[refusal_content]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_user_credential]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[schema_admin_credential]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[read_own_write]",
            "tests/test_release_1_0_api_metadata_oracles.py::test_metadata_oracles_detect_actual_http_and_persisted_faults[refusal_audit_delete]",
        ),
        (
            "api:GET /api/docs",
            "api:GET /api/openapi.json",
        ),
    ),
    "R10-INBOX-READ-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_inbox_read_oracles.py::test_self_inbox_exact_structural_cards_pages_and_private_exclusion[personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_admin_inbox_exact_target_content_pages_totals_and_audit[personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_refusals_preserve_content_state_and_exact_audit[self-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_refusals_preserve_content_state_and_exact_audit[admin-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[self_count-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[self_duplicate-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[admin_total-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[admin_raw-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[admin_extra_item-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[admin_extra_envelope-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[private_self-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[private_admin-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_401-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_403-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_title-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_credential-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_400-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_422-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[read_own-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[foreign_write-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[refusal_write-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[audit_missing-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[audit_actor-personal]",
            "tests/test_release_1_0_inbox_read_oracles.py::test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption[audit_prefix-personal]",
        ),
        (
            "api:GET /api/inbox",
            "api:GET /api/admin/inbox",
        ),
    ),
    "R10-PUBLIC-HEALTH-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_ok_with_storage_anon_owner_invalid_token[health]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_ok_with_storage_anon_owner_invalid_token[api_health]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_starting_without_storage_anon_owner_invalid_token[health]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_starting_without_storage_anon_owner_invalid_token[api_health]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-wrong_status]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-wrong_version]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-wrong_llm]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-wrong_secondary]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-wrong_vault]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-secret_in_200]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-own_body]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-foreign_body]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-own_ko]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-permission_write]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[health-audit_prefix]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-wrong_status]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-wrong_version]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-wrong_llm]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-wrong_secondary]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-wrong_vault]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-secret_in_200]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-own_body]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-foreign_body]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-own_ko]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-permission_write]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_corrupted_http_and_writes[api_health-audit_prefix]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_submitted_credential_in_200[health-owner]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_submitted_credential_in_200[health-invalid]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_submitted_credential_in_200[api_health-owner]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_submitted_credential_in_200[api_health-invalid]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_configured_path_in_200[health-home]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_configured_path_in_200[health-vault_path]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_configured_path_in_200[health-obsidian_root]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_configured_path_in_200[api_health-home]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_configured_path_in_200[api_health-vault_path]",
            "tests/test_release_1_0_public_health_oracles.py::test_public_health_oracles_detect_configured_path_in_200[api_health-obsidian_root]",
        ),
        (
            "api:GET /health",
            "api:GET /api/health",
        ),
    ),
    "R10-OPS-DIAGNOSTICS-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_owner_and_delegated_diagnostics_http_real_collector_privacy_and_no_effects",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_http_wrapper_forwards_check_llm_query_without_generation",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_http_anonymous_ordinary_revoked_and_malformed_refusals",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[missing_ok]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[missing_actions]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[wrong_features]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[missing_secondary_state]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[wrong_query_forwarding]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[secret_in_200]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[secret_in_refusal]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[own_ko]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[refusal_override]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[secret_in_403]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[secret_in_422]",
            "tests/test_release_1_0_ops_diagnostics_oracles.py::test_diagnostics_oracles_detect_corrupted_http_and_writes[audit_prefix]",
        ),
        ("api:GET /api/admin/diagnostics",),
    ),
    "R10-OPS-SETTINGS-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_ops_settings_oracles.py::test_owner_and_delegated_settings_http_closed_projection_privacy_and_no_effects",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_http_anonymous_ordinary_and_revoked_capability_refusals",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[missing_effective]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[wrong_auth_flag]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[secret_in_200]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[secret_in_refusal]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[secret_in_403]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[refusal_audit_prefix]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[own_ko]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[refusal_override]",
            "tests/test_release_1_0_ops_settings_oracles.py::test_settings_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:GET /api/admin/settings",),
    ),
    "R10-OPS-QUALITY-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_dashboard_binds_literal_totals_complete_pages_and_cross_person_audit[personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_refuses_missing_invalid_denied_revoked_and_anonymous_without_writes[personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[wrong_usage-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[wrong_feedback-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[wrong_graph-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[pressure_is_page_length-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[ignore_offset-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[missing_candidate-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[wrong_candidate_risk-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[missing_audit-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[wrong_audit_actor-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[wrong_audit_target-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[read_own_write-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[read_foreign_write-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[refusal_permission_write-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[refusal_secret-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[requested_reflection_401-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[requested_reflection_403-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[requested_reflection_404-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[requested_reflection_422-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[missing_person_audit-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[missing_person_actor-personal]",
            "tests/test_release_1_0_ops_quality_oracles.py::test_quality_oracles_detect_actual_http_audit_and_persisted_faults[missing_person_target-personal]",
            "tests/test_graph_runtime_log_privacy.py::test_private_knowledge_usage_is_neither_readable_mutable_nor_counted_by_admin",
        ),
        ("api:GET /api/admin/quality",),
    ),
    "R10-LIFECYCLE-STATS": (
        "deterministic",
        (
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_stats_literal_stages_and_audit",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[stages_count]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[own_ko]",
        ),
        ("api:GET /api/admin/lifecycle",),
    ),
    "R10-LIFECYCLE-CANDIDATES": (
        "deterministic",
        (
            "tests/test_numbers_that_saturated.py::test_the_lifecycle_candidates_route_reports_a_total",
            "tests/test_numbers_that_saturated.py::test_the_lifecycle_tile_counts_all_candidates_not_one_page",
            "tests/test_numbers_that_saturated.py::test_walking_the_lifecycle_pages_yields_each_candidate_once",
            "tests/test_numbers_that_saturated.py::test_the_apply_guard_sees_every_candidate",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:GET /api/admin/lifecycle/candidates",),
    ),
    "R10-LIFECYCLE-APPLY-ACTIONS": (
        "deterministic",
        (
            "tests/test_the_audit_log_never_takes_a_document_body.py::test_the_mutating_routes_write_a_fingerprint_not_the_text",
            "tests/test_numbers_that_saturated.py::test_the_apply_guard_sees_every_candidate",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_apply_archive_lower_keep_changes_and_skips",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_dropped]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_collateral]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[foreign_write]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_audit]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_lower_audit]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_keep_audit]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_keep_wrong]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_changed_row]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[apply_history_content]",
        ),
        ("api:POST /api/admin/lifecycle/apply",),
    ),
    "R10-LIFECYCLE-DEPRECATE": (
        "deterministic",
        (
            "tests/test_storage_and_lifecycle.py::test_mass_archive_without_selection_is_refused",
            "tests/test_hardening_regressions.py::test_admin_api_rejects_malformed_and_non_object_json_with_400",
            "tests/test_hardening_regressions.py::test_admin_api_rejects_invalid_scalar_types_with_400[/api/admin/lifecycle/deprecate-payload0-days_threshold: \\u043d\\u0443\\u0436\\u043d\\u043e \\u0446\\u0435\\u043b\\u043e\\u0435 \\u0447\\u0438\\u0441\\u043b\\u043e]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:POST /api/admin/lifecycle/deprecate",),
    ),
    "R10-CONFLICT-LIST-PAGE": (
        "deterministic",
        (
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_conflicts_list_exact_members_pages_count_total",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[conflict_members]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[conflict_total]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[conflict_read_audit]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:GET /api/admin/conflicts",),
    ),
    "R10-CONFLICT-RESOLVE": (
        "deterministic",
        (
            "tests/test_conflict_resolution.py::test_resolve_endpoint_gated_audited_and_wired",
            "tests/test_conflict_resolution.py::test_resolve_validates_winner_and_terminal_status",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:POST /api/admin/conflicts/{conflict_id}/resolve",),
    ),
    "R10-CONFLICT-REVIEW": (
        "deterministic",
        (
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_review_and_bulk_review_persisted_statuses_and_skips",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[review_dropped]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[review_row_collateral]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:POST /api/admin/conflicts/{conflict_id}/review",),
    ),
    "R10-CONFLICT-BULK-REVIEW": (
        "deterministic",
        (
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_review_and_bulk_review_persisted_statuses_and_skips",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_all_eight_routes_authority_target_refusal",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[bulk_dropped]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[bulk_audit]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[bulk_wrong]",
            "tests/test_release_1_0_lifecycle_conflict_oracles.py::test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes[refusal_link]",
        ),
        ("api:POST /api/admin/conflicts/bulk-review",),
    ),
    "R10-AUDIT-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_audit_oracles.py::test_audit_pages_bind_actor_filter_order_anchor_rows_and_read_provenance",
            "tests/test_release_1_0_audit_oracles.py::test_audit_refuses_anonymous_denied_revoked_and_invalid_reads_without_business_effect",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[wrong_actor_row]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[reversed_tie]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[wrong_total]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[ignored_anchor]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[row_payload]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[missing_read_audit]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[wrong_audit_actor]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[wrong_audit_target]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[read_business_write]",
            "tests/test_release_1_0_audit_oracles.py::test_audit_oracles_detect_real_http_storage_and_audit_faults[refusal_permission_write]",
        ),
        ("api:GET /api/admin/audit",),
    ),
    "R10-DATA-SOURCES-HTTP": (
        "deterministic",
        (
            "tests/test_release_1_0_data_source_oracles.py::test_source_reads_bind_exact_person_schema_and_audit_without_effects",
            "tests/test_release_1_0_data_source_oracles.py::test_source_declare_upsert_and_forget_have_closed_row_effects_and_audit",
            "tests/test_release_1_0_data_source_oracles.py::test_source_authority_and_invalid_declarations_preserve_business_state",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[list_foreign]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[schema_columns]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[schema_db_write]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[read_audit]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[created_by]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[replace_clock]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[foreign_delete]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[declare_audit]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[audit_target]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[declare_secret]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[invalid_reflection]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[anonymous_reflection]",
            "tests/test_release_1_0_data_source_oracles.py::test_source_oracles_reject_actual_response_storage_database_and_audit_faults[denied_reflection]",
        ),
        (
            "api:GET /api/admin/data-sources",
            "api:GET /api/admin/data-sources/{name}/schema",
            "api:POST /api/admin/data-sources",
            "api:DELETE /api/admin/data-sources/{name}",
        ),
    ),
    "R10-UI-ACTIVITY": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_activity_selected_person_period_preview_is_exact_and_read_only",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[response-activity]",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[write-activity]",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[audit-activity]",
        ),
        ("ui:activity",),
    ),
    "R10-UI-GRAPH": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_graph_selected_person_filter_and_focus_are_exact_and_read_only",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[response-graph]",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[write-graph]",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[audit-graph]",
        ),
        ("ui:graph",),
    ),
    "R10-UI-TIMELINE": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_timeline_selected_person_zoom_is_exact_and_read_only",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[response-timeline]",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[write-timeline]",
            "tests/test_release_1_0_ui_three_oracles.py::test_ui_three_oracles_reject_actual_response_write_and_audit_faults[audit-timeline]",
        ),
        ("ui:timeline",),
    ),
    "R10-UI-SOURCES": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_sources_lists_exact_person_and_never_carries_connection_secret",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_source_create_and_forget_preserve_same_named_foreign_source",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_source_missing_environment_schema_is_an_explicit_error",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_sources_oracle_rejects_real_http_and_persistence_faults[secret-ui_source_secret]",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_sources_oracle_rejects_real_http_and_persistence_faults[foreign-ui_source_members]",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_sources_oracle_rejects_real_http_and_persistence_faults[missing_create-ui_source_create_persisted]",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_sources_oracle_rejects_real_http_and_persistence_faults[undeleted-ui_source_forget_exact_effect]",
        ),
        ("ui:sources",),
    ),
    "R10-UI-CHATS": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_chat_reply_reaches_exact_person_and_two_clicks_queue_two_labelled_messages",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_chat_without_transport_has_no_reply_box_or_effect",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_chat_auto_refresh_shows_new_message_and_preserves_unsent_draft",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_chat_oracle_rejects_real_thread_and_delivery_queue_faults[wrong_thread-ui_chat_thread_members]",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_chat_oracle_rejects_real_thread_and_delivery_queue_faults[missing_reply-ui_chat_reply_count]",
            "tests/test_release_1_0_ui_sources_chats_oracles.py::test_ui_chat_oracle_rejects_real_thread_and_delivery_queue_faults[wrong_recipient-ui_chat_reply_target_and_body]",
        ),
        ("ui:chats",),
    ),
    "R10-UI-AUDIT": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_audit_lists_real_action_and_opens_matching_details",
            "tests/test_release_1_0_ui_oracles.py::test_ui_audit_details_do_not_show_a_different_row",
            "tests/test_release_1_0_ui_oracles.py::test_fault_wrong_audit_record_reds",
        ),
        ("ui:audit",),
    ),
    "R10-UI-BACKUPS": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_backups_create_now_lists_sha_and_persists",
            "tests/test_release_1_0_ui_oracles.py::test_ui_backups_verify_reaches_handler",
            "tests/test_release_1_0_ui_oracles.py::test_fault_backup_missing_artifact_reds_verify",
        ),
        ("ui:backups",),
    ),
    "R10-UI-CLEANUP": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_cleanup_empty_does_not_list_foreign",
            "tests/test_release_1_0_ui_oracles.py::test_fault_foreign_cleanup_http_member_reds_empty_person_oracle",
        ),
        ("ui:cleanup",),
    ),
    "R10-UI-COMPACTS": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_compacts_run_and_open_literal_counters",
            "tests/test_release_1_0_ui_oracles.py::test_fault_compact_visible_counter_reds_the_actual_result_oracle",
        ),
        ("ui:compacts",),
    ),
    "R10-UI-CONVERSATIONS": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_conversations_open_messages_and_archive_keeps_them",
            "tests/test_release_1_0_ui_oracles.py::test_fault_dropped_archive_write_reds",
        ),
        ("ui:conversations",),
    ),
    "R10-UI-DASHBOARD": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_dashboard_shows_literal_counts_and_honest_empty_backups",
            "tests/test_release_1_0_ui_oracles.py::test_ui_all_registered_tabs_are_in_the_sidebar",
            "tests/test_release_1_0_ui_oracles.py::test_ui_stack_cleans_resources_when_an_actual_setup_call_fails[seed]",
            "tests/test_release_1_0_ui_oracles.py::test_ui_stack_cleans_resources_when_an_actual_setup_call_fails[launch]",
            "tests/test_release_1_0_ui_oracles.py::test_ui_stack_cleans_resources_when_an_actual_setup_call_fails[goto]",
            "tests/test_release_1_0_ui_oracles.py::test_ui_stack_preserves_environment_failure_with_cleanup_failure",
            "tests/test_release_1_0_ui_oracles.py::test_ui_stack_normal_exit_closes_its_observed_browser_driver_and_socket",
            "tests/test_release_1_0_ui_oracles.py::test_fault_dashboard_wrong_user_count_reds",
        ),
        (
            "ui:dashboard",
            "api:MOUNT /admin",
        ),
    ),
    "R10-UI-DIAGNOSTICS": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_diagnostics_shows_live_llm_disabled_not_config_lie",
            "tests/test_release_1_0_ui_oracles.py::test_ui_nav_click_opens_diagnostics_title",
        ),
        ("ui:diagnostics",),
    ),
    "R10-UI-FILES": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_files_list_owned_hides_foreign",
            "tests/test_release_1_0_ui_oracles.py::test_ui_files_download_of_missing_bytes_errors_not_foreign",
        ),
        ("ui:files",),
    ),
    "R10-UI-INBOX": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_inbox_review_promotes_seeded_material_to_knowledge",
            "tests/test_release_1_0_ui_oracles.py::test_ui_inbox_hides_foreign_membership",
            "tests/test_release_1_0_ui_oracles.py::test_ui_empty_person_inbox_is_empty_not_alices",
            "tests/test_release_1_0_ui_oracles.py::test_ui_missing_token_is_auth_dialog_not_empty_pass",
        ),
        ("ui:inbox",),
    ),
    "R10-UI-KNOWLEDGE": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_knowledge_search_opens_inspection_of_seeded_document",
            "tests/test_release_1_0_ui_oracles.py::test_ui_knowledge_search_hides_foreign_document",
            "tests/test_release_1_0_ui_oracles.py::test_ui_knowledge_soft_deleted_inspection_keeps_own_history_and_leaves_the_list",
            "tests/test_release_1_0_ui_oracles.py::test_ui_wrong_token_does_not_leak_seeded_titles",
            "tests/test_release_1_0_ui_oracles.py::test_fault_wrong_search_hides_seeded_title_reds",
            "tests/test_release_1_0_ui_oracles.py::test_fault_suppressed_inspect_handler_reds",
        ),
        ("ui:knowledge",),
    ),
    "R10-UI-QUALITY": (
        "user-ui",
        ("tests/test_release_1_0_ui_oracles.py::test_ui_quality_explain_finds_seeded_title",),
        ("ui:quality",),
    ),
    "R10-UI-USERS": (
        "user-ui",
        (
            "tests/test_release_1_0_ui_oracles.py::test_ui_users_create_persists_literal_account",
            "tests/test_release_1_0_ui_oracles.py::test_ui_users_disable_does_not_change_sibling",
        ),
        ("ui:users",),
    ),
    "R10-ADMIN-FILES-PAGE": (
        "deterministic",
        (
            "tests/test_api_privacy_projections.py::test_user_and_admin_file_lists_apply_the_projection_at_the_http_boundary",
            "tests/test_admin_latency_boundaries.py::test_admin_file_page_does_not_block_the_event_loop",
            "tests/test_release_1_0_remaining_oracles.py::test_admin_lists_enforce_access_and_exact_pagination[files]",
        ),
        ("api:GET /api/admin/files", "api:GET /api/files"),
    ),
    "R10-ADMIN-ACCOUNT-LIST": (
        "deterministic",
        (
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_the_account_list_does_not_hand_over_what_a_person_wrote_about_themselves",
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_the_full_administrator_still_sees_the_metadata",
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_reading_the_account_list_without_full_access_is_recorded",
            "tests/test_release_1_0_remaining_oracles.py::test_admin_lists_enforce_access_and_exact_pagination[users]",
        ),
        ("api:GET /api/admin/users",),
    ),
    "R10-GRAPH-NEIGHBOURHOOD": (
        "deterministic",
        (
            "tests/test_known_at_graph_http.py::test_current_and_historical_neighbourhoods_publish_one_bounded_relation_shape",
            "tests/test_known_at_graph_http.py::test_direct_entity_surfaces_bound_relations_and_versions_before_serialization",
            "tests/test_known_at_graph_http.py::test_wide_entity_graph_is_bounded_allowlisted_and_honest_on_public_and_admin_http",
            "tests/test_release_1_0_graph_oracles.py::test_neighbourhood_http_oracle_distinguishes_current_from_known_at_and_exact_members",
            "tests/test_release_1_0_graph_oracles.py::test_neighbourhood_http_oracle_rejects_observed_result_mutations",
        ),
        (
            "api:GET /api/kg/graph/{entity_id}",
            "api:GET /api/admin/graph/{entity_id}",
            "api:GET /api/kg/entities/{entity_id}",
            "api:GET /api/kg/entity-profile",
        ),
    ),
    "R10-GRAPH-OVERVIEW": (
        "deterministic",
        (
            "tests/test_known_at_graph_http.py::test_real_admin_temporal_overview_echoes_normalized_date_and_relation_only_nodes",
            "tests/test_known_at_graph_http.py::test_current_admin_overview_reports_node_and_edge_truncation_honestly",
            "tests/test_release_1_0_graph_oracles.py::test_overview_http_oracle_has_exact_temporal_members_and_a_truly_empty_earlier_graph",
            "tests/test_release_1_0_graph_oracles.py::test_overview_http_oracle_rejects_observed_result_mutations",
        ),
        ("api:GET /api/admin/graph",),
    ),
    "R10-GRAPH-RELATION-CREATE": (
        "deterministic",
        (
            "tests/test_known_at_graph_http.py::test_relation_create_response_and_audit_use_separate_bounded_allowlists",
            "tests/test_release_1_0_graph_oracles.py::test_relation_create_binds_http_projection_to_exact_persisted_tuple_and_replay_audit",
        ),
        ("api:POST /api/kg/relations",),
    ),
    "R10-GRAPH-BULK-REVIEW": (
        "deterministic",
        (
            "tests/test_api_vertical_slice.py::test_admin_bulk_graph_review_is_bounded_and_reports_partial_failures",
            "tests/test_release_1_0_graph_oracles.py::test_bulk_review_binds_candidates_to_exact_edges_and_refusals_leave_state_unchanged",
        ),
        ("api:POST /api/admin/relation-candidates/bulk-review",),
    ),
    "R10-INBOX-GROUP-REVIEW": (
        "deterministic",
        (
            "tests/test_inbox_grouping.py::test_the_endpoint_returns_groups_and_the_available_axes",
            "tests/test_inbox_grouping.py::test_a_group_can_be_dismissed_but_not_promoted",
            "tests/test_release_1_0_graph_oracles.py::test_inbox_directory_groups_have_exact_membership_truncation_and_projection",
            "tests/test_release_1_0_graph_oracles.py::test_inbox_directory_group_oracle_rejects_observed_mutations",
        ),
        (
            "api:GET /api/admin/inbox/groups",
            "api:POST /api/admin/inbox/bulk",
        ),
    ),
    "R10-ASSISTANT-INBOX": (
        "deterministic",
        (
            "tests/test_api_vertical_slice.py::test_knowledge_work_result_can_only_enter_memory_through_inbox",
            "tests/test_release_1_0_graph_oracles.py::test_signed_assistant_candidate_has_exact_tenant_replay_rows_and_projection",
            "tests/test_release_1_0_graph_oracles.py::test_signed_assistant_candidate_oracle_rejects_observed_mutations",
        ),
        ("api:POST /api/assistant/candidates",),
    ),
    "R10-ADMIN-CHAT-FEED": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_chat_feed_has_exact_http_members_attribution_and_audit",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[feed_members]",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[feed_total]",
        ),
        ("api:GET /api/admin/chats",),
    ),
    "R10-ADMIN-CHAT-CURSOR": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_chat_cursor_tracks_actual_insert_and_has_no_content_audit",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[cursor_stale]",
        ),
        ("api:GET /api/admin/chats/cursor",),
    ),
    "R10-ADMIN-CHAT-THREAD": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_person_thread_spans_conversations_with_exact_http_window",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[thread_member]",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[thread_private]",
        ),
        ("api:GET /api/admin/chats/{user_id}/messages",),
    ),
    "R10-ADMIN-CHAT-CONVERSATION-PAGES": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_pages_bind_filter_archive_offset_and_exact_total",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[page_total]",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[page_tenant]",
        ),
        ("api:GET /api/admin/conversations",),
    ),
    "R10-ADMIN-CHAT-CONVERSATION-MESSAGES": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_transcript_has_exact_tail_offset_and_tenant_binding",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[transcript_offset]",
        ),
        ("api:GET /api/admin/conversations/{conversation_id}/messages",),
    ),
    "R10-ADMIN-CHAT-REPLY": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_reply_persists_two_exact_owned_queue_items_and_refusals_have_no_effect",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[reply_false]",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[reply_missing]",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[reply_foreign]",
        ),
        ("api:POST /api/admin/chats/{user_id}/reply",),
    ),
    "R10-ADMIN-CHAT-CONVERSATION-ARCHIVE": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_archive_delete_observe_persistence_history_channel_and_audit[archive]",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[archive_unwritten]",
        ),
        ("api:POST /api/admin/conversations/{conversation_id}/archive",),
    ),
    "R10-ADMIN-CHAT-CONVERSATION-DELETE": (
        "deterministic",
        (
            "tests/test_release_1_0_conversation_oracles.py::test_admin_archive_delete_observe_persistence_history_channel_and_audit[delete]",
            "tests/test_release_1_0_conversation_oracles.py::test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[delete_unwritten]",
            "tests/test_release_1_0_conversation_oracles.py::test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects[delete_audit_missing]",
        ),
        ("api:DELETE /api/admin/conversations/{conversation_id}",),
    ),
    "R10-PERMISSION-CATALOG": (
        "deterministic",
        (
            "tests/test_release_1_0_preset_oracles.py::test_capability_catalog_has_exact_membership_safe_metadata_and_no_effect",
            "tests/test_release_1_0_preset_oracles.py::test_anonymous_and_ordinary_accounts_cannot_read_or_mutate_preset_surfaces",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[capability_drop]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[capability_metadata]",
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_the_overseer_learns_the_shape_and_none_of_the_substance",
            "tests/test_an_organ_cannot_rewrite_a_system_right.py::test_every_living_organ_declares_where_it_came_from",
        ),
        ("api:GET /api/admin/capabilities",),
    ),
    "R10-PERMISSION-PRESET-LIST": (
        "deterministic",
        (
            "tests/test_release_1_0_preset_oracles.py::test_preset_catalog_has_exact_builtin_membership_and_persisted_custom_rows",
            "tests/test_release_1_0_preset_oracles.py::test_anonymous_and_ordinary_accounts_cannot_read_or_mutate_preset_surfaces",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[preset_membership]",
        ),
        ("api:GET /api/admin/presets",),
    ),
    "R10-PERMISSION-PRESET-UPSERT": (
        "deterministic",
        (
            "tests/test_release_1_0_preset_oracles.py::test_owner_create_deduplicates_persists_preserves_and_audits_private_fields",
            "tests/test_release_1_0_preset_oracles.py::test_assigned_preset_update_changes_subsequent_personal_http_authority_and_audits_before_after",
            "tests/test_release_1_0_preset_oracles.py::test_shared_archive_delegated_create_is_attributed_to_the_acting_person",
            "tests/test_release_1_0_preset_oracles.py::test_delegated_admin_cannot_rewrite_a_custom_preset_assigned_to_owner",
            "tests/test_release_1_0_preset_oracles.py::test_missing_invalid_and_builtin_preset_posts_are_refused_without_effect",
            "tests/test_release_1_0_preset_oracles.py::test_anonymous_and_ordinary_accounts_cannot_read_or_mutate_preset_surfaces",
            "tests/test_release_1_0_preset_oracles.py::test_delegated_admin_cannot_create_or_update_beyond_personal_authority",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[update_stale_authority]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_unwritten]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_wrong_capabilities]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_collateral_preset]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_collateral_account]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_response]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[audit_missing]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[audit_unsanitized]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[refusal_hidden_write]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_override]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[refusal_override]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_orphan_capability]",
            "tests/test_release_1_0_preset_oracles.py::test_preset_oracles_detect_real_http_output_storage_and_writer_faults[create_account_creation]",
            "tests/test_permissions_and_kernel.py::test_default_deny_presets_and_persistent_overrides",
            "tests/test_permissions_and_kernel.py::test_transactional_authorization_honours_custom_preset_and_active_status",
            "tests/test_delegation_hardening.py::test_custom_preset_creation_enforces_delegation",
            "tests/test_delegation_hardening.py::test_preset_assignment_enforces_delegation_at_service_level",
            "tests/test_api_vertical_slice.py::test_admin_delegation_cannot_escalate_to_owner",
            "tests/test_a_full_preset_is_not_the_archive_owner.py::test_a_participant_cannot_hand_out_the_owner_preset",
        ),
        ("api:POST /api/admin/presets",),
    ),
    "R10-ENTITY-REVIEW-QUEUE": (
        "deterministic",
        (
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_reads_observe_exact_membership_window_and_audit[queue]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_empty_windows_are_honest_without_writing[queue]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[accepted]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[rejected]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[suggested]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_requires_authority_and_respects_explicit_denial[queue]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_invalid_windows_are_refused_without_any_effect[queue]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[queue_total]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[queue_foreign]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[queue_estimate]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[refusal_write]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[audit_actor]",
            "tests/test_the_suggestion_queue_exists.py::test_documents_with_pending_suggestions_are_listed_by_weight",
            "tests/test_the_suggestion_queue_exists.py::test_confirmed_links_reduce_what_is_left",
            "tests/test_the_suggestion_queue_exists.py::test_a_fully_resolved_document_leaves_the_queue",
            "tests/test_the_suggestion_queue_exists.py::test_a_rejected_link_also_counts_as_decided",
            "tests/test_the_suggestion_queue_exists.py::test_a_document_without_the_stored_count_is_not_offered",
        ),
        ("api:GET /api/admin/entity-suggestions/queue",),
    ),
    "R10-ENTITY-REVIEW-GROUPS": (
        "deterministic",
        (
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_reads_observe_exact_membership_window_and_audit[groups]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_empty_windows_are_honest_without_writing[groups]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_sentence_final_period_keeps_a_literal_existing_name[groups]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[accepted]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[rejected]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[suggested]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_requires_authority_and_respects_explicit_denial[groups]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_invalid_windows_are_refused_without_any_effect[groups]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[group_member]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[group_window]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[read_write]",
            "tests/test_the_suggestion_queue_exists.py::test_groups_collect_one_entity_across_documents",
        ),
        ("api:GET /api/admin/entity-suggestions/groups",),
    ),
    "R10-ENTITY-REVIEW-SUGGESTIONS": (
        "deterministic",
        (
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_reads_observe_exact_membership_window_and_audit[suggestions]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_empty_windows_are_honest_without_writing[suggestions]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_sentence_final_period_keeps_a_literal_existing_name[suggestions]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[accepted]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[rejected]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_existing_decisions_are_not_offered_again[suggested]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_requires_authority_and_respects_explicit_denial[suggestions]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_invalid_windows_are_refused_without_any_effect[suggestions]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_document_suggestions_refuse_wrong_deleted_and_missing_targets[local:r10-profile-b-first]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_document_suggestions_refuse_wrong_deleted_and_missing_targets[local:r10-profile-a-deleted]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_document_suggestions_refuse_wrong_deleted_and_missing_targets[local:r10-profile-a-missing]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[suggestion_order]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[suggestion_foreign]",
            "tests/test_release_1_0_entity_queue_oracles.py::test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults[missing_audit]",
        ),
        ("api:GET /api/admin/knowledge/{knowledge_id}/entity-suggestions",),
    ),
    "R10-KNOWLEDGE-MUTATION-EDIT": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_persist_exact_outcomes_history_and_personal_audit[edit]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_refuse_anonymous_ordinary_and_explicit_denial_without_effect[edit]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_protect_owner_and_refuse_wrong_target_without_effect[edit]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_invalid_input_and_active_purge_cannot_change_business_rows[edit]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[edit_unwritten]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[edit_collateral]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[edit_response]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[history_damage]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[audit_missing]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[audit_actor]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[refusal_write]",
        ),
        ("api:PATCH /api/admin/knowledge/{knowledge_id}",),
    ),
    "R10-KNOWLEDGE-MUTATION-RESTORE": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_persist_exact_outcomes_history_and_personal_audit[restore]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_refuse_anonymous_ordinary_and_explicit_denial_without_effect[restore]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_protect_owner_and_refuse_wrong_target_without_effect[restore]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_invalid_input_and_active_purge_cannot_change_business_rows[restore]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[restore_wrong_version]",
            "tests/test_a_mistaken_edit_can_be_undone.py::test_old_snapshots_compress_in_place_and_undo_survives",
            "tests/test_a_mistaken_edit_can_be_undone.py::test_the_route_refuses_a_missing_version",
            "tests/test_a_rollback_is_signed_by_a_person.py::test_in_a_shared_archive_the_signature_tells_people_apart",
        ),
        ("api:POST /api/admin/knowledge/{knowledge_id}/restore",),
    ),
    "R10-KNOWLEDGE-MUTATION-DELETE": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_persist_exact_outcomes_history_and_personal_audit[delete]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_refuse_anonymous_ordinary_and_explicit_denial_without_effect[delete]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_protect_owner_and_refuse_wrong_target_without_effect[delete]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[delete_unwritten]",
        ),
        ("api:DELETE /api/admin/knowledge/{knowledge_id}",),
    ),
    "R10-KNOWLEDGE-MUTATION-PURGE": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_persist_exact_outcomes_history_and_personal_audit[purge]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_refuse_anonymous_ordinary_and_explicit_denial_without_effect[purge]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_protect_owner_and_refuse_wrong_target_without_effect[purge]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_invalid_input_and_active_purge_cannot_change_business_rows[purge]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[purge_unwritten]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[purge_count]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[purge_repeat_collateral]",
            "tests/test_purge.py::test_purge_removes_every_trace_including_fts",
            "tests/test_purge.py::test_purge_removes_transport_alias_before_its_raw_target",
            "tests/test_purge.py::test_purge_deletes_raw_file_and_vault_copy",
            "tests/test_purge.py::test_purge_keeps_a_deduplicated_file_until_the_last_reference",
            "tests/test_memory_vault_containment.py::test_disabled_admin_purge_uses_deletion_only_handle_and_removes_crash_temp",
            "tests/test_memory_vault_containment.py::test_disabled_admin_purge_blocks_before_db_commit_when_legacy_unlink_fails",
            "tests/test_memory_vault_containment.py::test_admin_purge_reports_committed_cleanup_when_completion_audit_fails",
            "tests/test_memory_vault_containment.py::test_admin_purge_does_not_start_when_attempt_audit_is_unavailable",
        ),
        ("api:POST /api/admin/knowledge/{knowledge_id}/purge",),
    ),
    "R10-KNOWLEDGE-MUTATION-PURGEABLE": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_persist_exact_outcomes_history_and_personal_audit[purgeable]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutations_refuse_anonymous_ordinary_and_explicit_denial_without_effect[purgeable]",
            "tests/test_release_1_0_knowledge_mutation_oracles.py::test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults[purgeable_foreign]",
            "tests/test_purge.py::test_list_purgeable_respects_retention_window",
        ),
        ("api:GET /api/admin/data/purgeable",),
    ),
    "R10-KNOWLEDGE-READ-LIST": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[list]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[list]",
            "tests/test_admin_lists_page_honestly.py::test_the_knowledge_route_returns_a_total_that_respects_the_filter",
            "tests/test_finding_a_document_by_hand.py::test_the_admin_route_passes_the_query_through",
            "tests/test_containers_browse.py::test_admin_tags_containers_and_entity_filter",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[filtered_total]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[missing_audit]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[read_write]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[refusal_write]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_preservation_catches_an_actual_relation_written_after_http[read]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_preservation_catches_an_actual_relation_written_after_http[refusal]",
        ),
        ("api:GET /api/admin/knowledge",),
    ),
    "R10-KNOWLEDGE-READ-TAGS": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[tags]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[tags]",
            "tests/test_containers_browse.py::test_admin_tags_containers_and_entity_filter",
        ),
        ("api:GET /api/admin/knowledge/tags",),
    ),
    "R10-KNOWLEDGE-READ-TIMELINE": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[timeline]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[timeline]",
            "tests/test_corpus_timeline.py::test_histogram_groups_by_year_month_and_day",
            "tests/test_corpus_timeline.py::test_documents_without_their_own_date_are_counted_not_hidden",
            "tests/test_corpus_timeline.py::test_histogram_is_scoped_to_one_account",
            "tests/test_corpus_timeline.py::test_timeline_endpoint_answers_and_picks_granularity",
            "tests/test_corpus_timeline.py::test_timeline_path_is_not_swallowed_by_the_inspect_route",
        ),
        ("api:GET /api/admin/knowledge/timeline",),
    ),
    "R10-KNOWLEDGE-READ-INSPECT": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[inspect]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[inspect]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[missing_history]",
        ),
        ("api:GET /api/admin/knowledge/{knowledge_id}",),
    ),
    "R10-KNOWLEDGE-READ-DIFF": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[diff]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[diff]",
            "tests/test_version_diff.py::test_diff_snapshots_by_field_kind",
            "tests/test_version_diff.py::test_diff_snapshots_no_changes_is_empty",
            "tests/test_version_diff.py::test_diff_defaults_to_two_most_recent",
            "tests/test_version_diff.py::test_diff_single_version_has_no_changes",
            "tests/test_version_diff.py::test_diff_endpoint",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[wrong_diff]",
        ),
        ("api:GET /api/admin/knowledge/{knowledge_id}/diff",),
    ),
    "R10-KNOWLEDGE-READ-MENTIONS": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[mentions]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[mentions]",
            "tests/test_entity_mentions_in_text.py::test_the_route_marks_only_confirmed_entities",
            "tests/test_entity_mentions_in_text.py::test_reading_someone_elses_document_is_audited",
        ),
        ("api:GET /api/admin/knowledge/{knowledge_id}/entity-mentions",),
    ),
    "R10-KNOWLEDGE-READ-SOURCE": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[source]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[source]",
            "tests/test_finding_a_document_by_hand.py::test_source_search_is_reachable_over_http",
            "tests/test_finding_a_document_by_hand.py::test_source_search_requires_the_admin_capability",
            "tests/test_finding_a_document_by_hand.py::test_the_page_length_is_not_presented_as_a_total",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes[wrong_source]",
        ),
        ("api:GET /api/admin/source-search",),
    ),
    "R10-KNOWLEDGE-READ-CONTAINERS": (
        "deterministic",
        (
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit[containers]",
            "tests/test_release_1_0_knowledge_read_oracles.py::test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial[containers]",
            "tests/test_containers_browse.py::test_admin_tags_containers_and_entity_filter",
        ),
        ("api:GET /api/admin/containers",),
    ),
    "R10-CHRONICLE-WINDOW": (
        "deterministic",
        (
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_preserves_shared_corpus_and_each_persons_private_calendar",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_recent_window_compares_utc_records_to_the_same_instant",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_includes_a_timed_event_earlier_today_in_the_persons_calendar",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_anniversaries_use_local_day_exact_order_and_five_item_limit",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_empty_history_is_an_exact_empty_result",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_fifty_item_limits_count_visible_rows_and_preserve_order",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_rejects_anonymous_denied_guest_and_invalid_windows_without_effects",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects[foreign_event]",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects[missing_record]",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects[wrong_order]",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects[read_write]",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects[refusal_write]",
            "tests/test_release_1_0_chronicle_oracles.py::test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects[wrong_anniversary]",
            "tests/test_organs_profile_chronicle.py::test_chronicle_window_endpoint",
            "tests/test_the_anniversary_is_in_the_persons_day.py::test_a_late_evening_record_keeps_the_persons_date",
            "tests/test_the_anniversary_is_in_the_persons_day.py::test_it_does_not_show_up_a_day_early",
            "tests/test_the_anniversary_is_in_the_persons_day.py::test_a_daytime_record_is_unaffected",
            "tests/test_the_anniversary_is_in_the_persons_day.py::test_a_naive_moment_does_not_break_the_call",
        ),
        ("api:GET /api/chronicle",),
    ),
    "R10-ME-PERSON": (
        "deterministic",
        (
            "tests/test_release_1_0_profile_oracles.py::test_instructions_patch_and_clear_affect_only_the_authenticated_person[personal]",
            "tests/test_release_1_0_profile_oracles.py::test_instructions_patch_and_clear_affect_only_the_authenticated_person[shared]",
            "tests/test_release_1_0_profile_oracles.py::test_guest_can_set_bounded_style_without_gaining_authority",
            "tests/test_release_1_0_profile_oracles.py::test_profile_and_instructions_require_their_actual_capabilities",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[me_person]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[me_write]",
        ),
        ("api:GET /api/me",),
    ),
    "R10-PERSONAL-INSTRUCTIONS": (
        "deterministic",
        (
            "tests/test_release_1_0_profile_oracles.py::test_instructions_patch_and_clear_affect_only_the_authenticated_person[personal]",
            "tests/test_release_1_0_profile_oracles.py::test_instructions_patch_and_clear_affect_only_the_authenticated_person[shared]",
            "tests/test_release_1_0_profile_oracles.py::test_guest_can_set_bounded_style_without_gaining_authority",
            "tests/test_release_1_0_profile_oracles.py::test_profile_and_instructions_require_their_actual_capabilities",
            "tests/test_release_1_0_profile_oracles.py::test_saved_personal_style_reaches_only_its_person_in_untrusted_prompt_data[personal]",
            "tests/test_release_1_0_profile_oracles.py::test_saved_personal_style_reaches_only_its_person_in_untrusted_prompt_data[shared]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[unwritten]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[collateral]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[response]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[refusal_write]",
        ),
        ("api:PATCH /api/me/instructions",),
    ),
    "R10-DERIVED-PROFILE": (
        "deterministic",
        (
            "tests/test_release_1_0_profile_oracles.py::test_profile_reflects_exact_authorized_corpus_and_visible_facts_without_writes[personal]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_reflects_exact_authorized_corpus_and_visible_facts_without_writes[shared]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_optional_synthesis_is_bounded_readonly_and_falls_back[portrait]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_optional_synthesis_is_bounded_readonly_and_falls_back[provider_failure]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_and_instructions_require_their_actual_capabilities",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[profile_model]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[profile_message]",
            "tests/test_release_1_0_profile_oracles.py::test_profile_oracles_detect_actual_response_and_persistence_corruption[profile_write]",
        ),
        ("api:GET /api/profile",),
    ),
    "R10-IDENTITY-LIST": (
        "deterministic",
        (
            "tests/test_release_1_0_identity_oracles.py::test_identity_listing_has_exact_global_and_person_filters_counts_order_and_audit",
            "tests/test_release_1_0_identity_oracles.py::test_identity_owner_filter_audit_keeps_the_person_target_in_a_shared_archive",
            "tests/test_release_1_0_identity_oracles.py::test_identity_endpoints_refuse_anonymous_ordinary_and_delegated_owner_mutations_without_effects",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[refusal_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[refusal_override]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[list_filter]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[list_output]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[list_audit]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[owner_list_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[owner_list_override]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[owner_list_other_identity]",
        ),
        ("api:GET /api/admin/identities",),
    ),
    "R10-IDENTITY-LINK": (
        "deterministic",
        (
            "tests/test_release_1_0_identity_oracles.py::test_identity_link_and_repeat_persist_one_person_binding_preserve_siblings_and_audit",
            "tests/test_release_1_0_identity_oracles.py::test_owner_reassignment_moves_only_the_requested_identity_and_audits_the_previous_person",
            "tests/test_release_1_0_identity_oracles.py::test_shared_archive_link_is_attributed_to_the_acting_person",
            "tests/test_release_1_0_identity_oracles.py::test_delegated_admin_cannot_reassign_an_owner_bound_identity",
            "tests/test_release_1_0_identity_oracles.py::test_identity_invalid_requests_have_no_persisted_or_success_audit_effect",
            "tests/test_release_1_0_identity_oracles.py::test_identity_endpoints_refuse_anonymous_ordinary_and_delegated_owner_mutations_without_effects",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[refusal_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[refusal_override]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[link_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[link_unwritten]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[link_wrong_person]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[link_collateral]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[link_response]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[link_audit]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[link_repeat_provenance]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[invalid_false_refusal]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[delegated_link_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[delegated_link_override]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[owner_rebind_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[owner_rebind_override]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures[delegated_link_other_identity]",
        ),
        ("api:POST /api/admin/identities",),
    ),
    "R10-IDENTITY-UNLINK": (
        "deterministic",
        (
            "tests/test_release_1_0_identity_oracles.py::test_identity_unlink_repeat_and_missing_remove_once_preserve_siblings_and_audit",
            "tests/test_release_1_0_identity_oracles.py::test_identity_endpoints_refuse_anonymous_ordinary_and_delegated_owner_mutations_without_effects",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[refusal_account]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_review_oracles_catch_real_collateral_authority_mutations[refusal_override]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[unlink_unwritten]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[unlink_collateral]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[unlink_response]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[unlink_audit]",
            "tests/test_release_1_0_identity_oracles.py::test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations[missing_false_refusal]",
        ),
        ("api:DELETE /api/admin/identities/{source}/{external_id}",),
    ),
    "R10-ACCOUNT-RESOLVE": (
        "deterministic",
        (
            "tests/test_release_1_0_account_oracles.py::test_account_resolution_returns_exact_people_and_no_arbitrary_ambiguous_winner",
            "tests/test_release_1_0_account_oracles.py::test_account_http_authority_validation_and_missing_target_refusals_have_no_account_effect",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[refusal_effect]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[resolve_winner]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[resolve_private]",
        ),
        ("api:GET /api/admin/users/resolve",),
    ),
    "R10-ACCOUNT-PRESET": (
        "deterministic",
        (
            "tests/test_release_1_0_account_oracles.py::test_account_preset_changes_persist_and_change_actual_personal_http_authority",
            "tests/test_release_1_0_account_oracles.py::test_account_authority_mutations_preserve_nonlocal_origin[preset]",
            "tests/test_release_1_0_account_oracles.py::test_account_http_authority_validation_and_missing_target_refusals_have_no_account_effect",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[refusal_effect]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[preset_unwritten]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[preset_collateral]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[preset_auth]",
        ),
        ("api:POST /api/admin/users/{user_id}/preset",),
    ),
    "R10-ACCOUNT-SUPERVISOR": (
        "deterministic",
        (
            "tests/test_release_1_0_account_oracles.py::test_account_supervisor_http_sets_clears_and_enforces_hierarchy_with_cycle_refusals",
            "tests/test_release_1_0_account_oracles.py::test_account_http_authority_validation_and_missing_target_refusals_have_no_account_effect",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[refusal_effect]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[supervisor_metadata]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[supervisor_scope]",
        ),
        ("api:POST /api/admin/users/{user_id}/supervisor",),
    ),
    "R10-ACCOUNT-PERMISSION": (
        "deterministic",
        (
            "tests/test_release_1_0_account_oracles.py::test_account_allow_deny_inherit_preserve_other_accounts_and_change_real_http",
            "tests/test_release_1_0_account_oracles.py::test_account_authority_mutations_preserve_nonlocal_origin[permission]",
            "tests/test_release_1_0_account_oracles.py::test_account_http_authority_validation_and_missing_target_refusals_have_no_account_effect",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[refusal_effect]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[permission_unwritten]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[permission_auth]",
        ),
        ("api:PUT /api/admin/users/{user_id}/permissions/{security_id}",),
    ),
    "R10-ACCOUNT-ACTIVITY": (
        "deterministic",
        (
            "tests/test_release_1_0_account_oracles.py::test_account_activity_shared_tenant_filters_author_pages_content_and_personal_audit",
            "tests/test_release_1_0_account_oracles.py::test_account_supervisor_http_sets_clears_and_enforces_hierarchy_with_cycle_refusals",
            "tests/test_release_1_0_account_oracles.py::test_account_http_authority_validation_and_missing_target_refusals_have_no_account_effect",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[refusal_effect]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[activity_author]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[activity_private]",
            "tests/test_release_1_0_account_oracles.py::test_account_oracles_detect_real_http_and_persisted_faults[activity_audit]",
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_the_overseer_learns_the_shape_and_none_of_the_substance",
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_the_full_administrator_lost_nothing",
            "tests/test_oversight_tier_sees_shape_not_substance.py::test_the_log_says_which_authority_answered",
            "tests/test_requested_analysis.py::test_the_route_carries_the_analysis_and_records_it",
        ),
        ("api:GET /api/admin/users/{user_id}/activity",),
    ),
    "R10-USER-CREATE": (
        "deterministic",
        (
            "tests/test_release_1_0_user_oracles.py::test_user_create_and_repeat_upsert_have_exact_person_fields_preservation_and_audit",
            "tests/test_release_1_0_user_oracles.py::test_user_create_preserves_explicit_source_and_external_identity",
            "tests/test_release_1_0_user_oracles.py::test_user_create_patch_invalid_inputs_and_missing_person_have_no_persisted_effect",
            "tests/test_release_1_0_user_oracles.py::test_user_administration_refuses_anonymous_ordinary_and_delegated_owner_mutation",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[create_fields]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[create_response]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[create_collateral]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[create_audit]",
        ),
        ("api:POST /api/admin/users",),
    ),
    "R10-USER-UPDATE": (
        "deterministic",
        (
            "tests/test_release_1_0_user_oracles.py::test_user_patch_persists_exact_fields_and_disable_reactivate_changes_actual_auth",
            "tests/test_release_1_0_user_oracles.py::test_user_create_patch_invalid_inputs_and_missing_person_have_no_persisted_effect",
            "tests/test_release_1_0_user_oracles.py::test_user_administration_refuses_anonymous_ordinary_and_delegated_owner_mutation",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[patch_unwritten]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[patch_collateral]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[patch_preset]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[patch_auth]",
            "tests/test_release_1_0_user_oracles.py::test_user_oracles_detect_corrupted_http_and_actual_account_mutations[invalid_false_refusal]",
        ),
        ("api:PATCH /api/admin/users/{user_id}",),
    ),
    "R10-USER-DELETION-PREFLIGHT": (
        "deterministic",
        (
            "tests/test_admin_user_deletion.py::test_disabled_account_is_erased_with_access_sessions_data_and_audit",
            "tests/test_admin_user_deletion.py::test_confirmation_and_fingerprint_are_both_fail_closed",
            "tests/test_admin_user_deletion.py::test_delete_waits_for_cli_export_or_backup_contour",
            "tests/test_admin_user_deletion.py::test_disabled_hard_delete_surface_cannot_mutate_an_account",
            "tests/test_admin_user_deletion.py::test_hard_delete_is_code_owned_disabled_even_if_an_env_escape_is_attempted",
            "tests/test_admin_user_deletion.py::test_shared_tenant_authorship_blocks_without_touching_either_account",
            "tests/test_release_1_0_user_oracles.py::test_user_deletion_get_and_delete_enforce_actual_authority_without_account_effects",
            "tests/test_release_1_0_user_oracles.py::test_user_delete_refusal_oracle_detects_a_real_write_after_the_http_refusal",
            "tests/test_admin_user_deletion.py::test_preflight_names_missing_worker_off_maintenance_proof",
            "tests/test_admin_user_deletion.py::test_delete_routes_require_management_and_protect_self_owner_and_system",
            "tests/test_admin_user_deletion.py::test_immutable_chat_history_is_reported_instead_of_bypassed",
        ),
        ("api:GET /api/admin/users/{user_id}/deletion",),
    ),
    "R10-USER-DELETE": (
        "deterministic",
        (
            "tests/test_admin_user_deletion.py::test_disabled_account_is_erased_with_access_sessions_data_and_audit",
            "tests/test_admin_user_deletion.py::test_confirmation_and_fingerprint_are_both_fail_closed",
            "tests/test_admin_user_deletion.py::test_delete_waits_for_cli_export_or_backup_contour",
            "tests/test_admin_user_deletion.py::test_disabled_hard_delete_surface_cannot_mutate_an_account",
            "tests/test_admin_user_deletion.py::test_hard_delete_is_code_owned_disabled_even_if_an_env_escape_is_attempted",
            "tests/test_admin_user_deletion.py::test_shared_tenant_authorship_blocks_without_touching_either_account",
            "tests/test_release_1_0_user_oracles.py::test_user_deletion_get_and_delete_enforce_actual_authority_without_account_effects",
            "tests/test_release_1_0_user_oracles.py::test_user_delete_refusal_oracle_detects_a_real_write_after_the_http_refusal",
        ),
        ("api:DELETE /api/admin/users/{user_id}",),
    ),
    "R10-REMINDER-LIST": (
        "deterministic",
        (
            "tests/test_release_1_0_reminder_oracles.py::test_get_reminders_has_exact_person_order_window_and_durable_state",
            "tests/test_release_1_0_reminder_oracles.py::test_get_reminders_refuses_anonymous_without_observing_or_mutating_private_rows",
            "tests/test_release_1_0_reminder_oracles.py::test_dismissed_head_does_not_consume_the_active_http_limit",
            "tests/test_release_1_0_reminder_oracles.py::test_reminder_list_preserves_an_exact_personal_clock",
            "tests/test_release_1_0_reminder_oracles.py::test_list_oracle_rejects_mutated_actual_http_output[order]",
            "tests/test_release_1_0_reminder_oracles.py::test_list_oracle_rejects_mutated_actual_http_output[foreign-id]",
            "tests/test_release_1_0_reminder_oracles.py::test_list_oracle_rejects_mutated_actual_http_output[state]",
        ),
        ("api:GET /api/me/reminders",),
    ),
    "R10-REMINDER-DISMISS": (
        "deterministic",
        (
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_accepts_queued_id_and_prescan_entity_then_blocks_every_rescan",
            "tests/test_release_1_0_reminder_oracles.py::test_every_current_listed_delivery_edge_state_remains_dismissible[uncertain-event-id]",
            "tests/test_release_1_0_reminder_oracles.py::test_every_current_listed_delivery_edge_state_remains_dismissible[failed-event-id]",
            "tests/test_release_1_0_reminder_oracles.py::test_legacy_sent_queue_id_button_remains_dismissible_after_delivery",
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_refusals_have_no_cross_person_effect[foreign-event]",
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_refusals_have_no_cross_person_effect[foreign-queue]",
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_refusals_have_no_cross_person_effect[missing]",
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_refusals_have_no_cross_person_effect[anonymous]",
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_oracle_rejects_http_success_without_persisted_effect[queued-write-missing]",
            "tests/test_release_1_0_reminder_oracles.py::test_dismiss_oracle_rejects_http_success_without_persisted_effect[prescan-write-missing]",
            "tests/test_reminders_self_service.py::test_dismiss_works_after_the_reminder_was_already_sent",
            "tests/test_reminders_self_service.py::test_dismiss_notification_rejects_non_reminder_kind",
        ),
        ("api:POST /api/me/reminders/{notification_id}/dismiss",),
    ),
    "R10-REMINDER-TELEGRAM": (
        "deterministic",
        (
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminder_buttons_never_exceed_the_transport_limit",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_long_reminder_oracle_rejects_actual_dropped_body",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminders_actual_command_renders_own_ids_and_dismisses_via_http",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminder_callback_refuses_unowned_ids_without_cross_person_effect[foreign]",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminder_callback_refuses_unowned_ids_without_cross_person_effect[missing]",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminder_callback_preserves_backend_forbidden_refusal",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminders_refuse_an_unallowed_chat_before_backend[command]",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminders_refuse_an_unallowed_chat_before_backend[callback]",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminder_oracle_rejects_mutated_transport_output[button-missing]",
            "tests/test_release_1_0_reminder_oracles.py::test_telegram_reminder_oracle_rejects_mutated_transport_output[button-foreign]",
        ),
        ("telegram:reminders",),
    ),
    "R10-REMINDER-DELIVERY-MIGRATION": (
        "deterministic",
        (
            "tests/test_release_1_0_reminder_oracles.py::test_dismissal_tombstone_survives_storage_delivery_chat_metadata_transition",
        ),
        (),
    ),
    "R10-TOKEN-LIST": (
        "deterministic",
        (
            "tests/test_release_1_0_token_oracles.py::test_token_listing_has_exact_filters_order_revocation_and_safe_metadata",
            "tests/test_release_1_0_token_oracles.py::test_token_management_refuses_anonymous_ordinary_and_owner_escalation_without_target_changes",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[list_filter]",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[list_hash]",
            "tests/test_release_1_0_token_oracles.py::test_delegated_admin_token_listing_has_exact_safe_metadata_and_only_its_auth_touch",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[delegated_list]",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[delegated_touch]",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[ordinary_touch]",
        ),
        ("api:GET /api/admin/tokens",),
    ),
    "R10-TOKEN-MINT": (
        "deterministic",
        (
            "tests/test_release_1_0_token_oracles.py::test_token_mint_persists_one_hash_exact_ttl_audit_and_authenticates_bound_person",
            "tests/test_release_1_0_token_oracles.py::test_token_mint_refuses_fractional_ttl_without_persisting_a_credential",
            "tests/test_release_1_0_token_oracles.py::test_token_management_refuses_anonymous_ordinary_and_owner_escalation_without_target_changes",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[mint_missing]",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[mint_foreign]",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[mint_secret]",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[mint_ttl]",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[ttl_false_refusal]",
            "tests/test_api_tokens.py::test_delegated_admin_cannot_mint_owner_token",
            "tests/test_api_tokens.py::test_admin_mint_with_ttl_sets_expiry_and_rejects_bad_ttl",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[mint_touch]",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[admin_touch]",
        ),
        ("api:POST /api/admin/tokens",),
    ),
    "R10-TOKEN-REVOKE": (
        "deterministic",
        (
            "tests/test_release_1_0_token_oracles.py::test_token_revocation_preserves_sibling_auth_and_repeat_cannot_mutate_again",
            "tests/test_release_1_0_token_oracles.py::test_token_management_refuses_anonymous_ordinary_and_owner_escalation_without_target_changes",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[revoke_unwritten]",
            "tests/test_release_1_0_token_oracles.py::test_token_oracles_detect_corrupt_http_and_real_persistence_effects[revoke_foreign]",
            "tests/test_api_tokens.py::test_delegated_admin_cannot_revoke_an_owner_token",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[revoke_missing_audit]",
            "tests/test_release_1_0_token_oracles.py::test_token_review_controls_catch_scope_audit_and_delegated_listing_faults[revoked_auth]",
        ),
        ("api:DELETE /api/admin/tokens/{token_id}",),
    ),
    "R10-SELF-CONVERSATION-LIST": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_listing_keeps_personal_membership_and_archive_visibility_in_shared_archive",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[listing_foreign]",
        ),
        ("api:GET /api/conversations",),
    ),
    "R10-SELF-CONVERSATION-HISTORY": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_history_has_exact_tail_and_foreign_owner_cannot_read_it",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[history_foreign]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_current_reference_requires_signed_bridge_and_binds_literal_person_channel",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[current_foreign_message]",
        ),
        ("api:GET /api/conversations/{conversation_id}/messages",),
    ),
    "R10-SELF-CONVERSATION-EXPORT": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_export_returns_exact_download_bytes_order_and_truncation_window",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[export_wrong_bytes]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_current_reference_requires_signed_bridge_and_binds_literal_person_channel",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[current_foreign_message]",
        ),
        ("api:GET /api/conversations/{conversation_id}/export",),
    ),
    "R10-SELF-CONVERSATION-SEARCH": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_search_binds_query_limit_conversation_filter_and_actual_message_ids",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[search_foreign]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[search_no_conversation]",
        ),
        ("api:GET /api/me/messages/search",),
    ),
    "R10-SELF-CONVERSATION-RESET": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_reset_clears_only_own_channel_and_never_deletes_history",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[reset_unwritten]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[reset_foreign]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_reset_oracle_rejects_actual_overbroad_session_deletion[user_id]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_reset_oracle_rejects_actual_overbroad_session_deletion[channel]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_reset_oracle_rejects_actual_overbroad_session_deletion[channel_id]",
        ),
        ("api:POST /api/conversations/channel/reset",),
    ),
    "R10-SELF-CONVERSATION-WHY": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_why_selects_own_latest_assistant_and_resolves_shared_knowledge_safely",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[why_wrong_query]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[why_wrong_title]",
        ),
        ("api:GET /api/conversations/channel/why",),
    ),
    "R10-SELF-CONVERSATION-RENAME": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_mutation_preserves_person_boundary_history_and_channel_state[rename]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[rename_unwritten]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_current_reference_requires_signed_bridge_and_binds_literal_person_channel",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[current_foreign_message]",
        ),
        ("api:PATCH /api/conversations/{conversation_id}",),
    ),
    "R10-SELF-CONVERSATION-ARCHIVE": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_mutation_preserves_person_boundary_history_and_channel_state[archive]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[archive_unarchive_unwritten]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_current_reference_requires_signed_bridge_and_binds_literal_person_channel",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[current_foreign_message]",
        ),
        ("api:POST /api/conversations/{conversation_id}/archive",),
    ),
    "R10-SELF-CONVERSATION-DELETE": (
        "deterministic",
        (
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_mutation_preserves_person_boundary_history_and_channel_state[delete]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_anonymous_self_conversation_calls_have_no_personal_state_effect",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[delete_unwritten]",
            "tests/test_release_1_0_self_conversation_oracles.py::test_current_reference_requires_signed_bridge_and_binds_literal_person_channel",
            "tests/test_release_1_0_self_conversation_oracles.py::test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes[current_foreign_message]",
        ),
        ("api:DELETE /api/conversations/{conversation_id}",),
    ),
}
_API_DECORATOR_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_LEGACY_API_DECORATOR_RECEIVERS = frozenset({"application", "router", "admin_router", "app"})
_CLI_PARSER = re.compile(r"add_parser\(\s*[\"']([a-z0-9-]+)")
_UI_VIEWS = re.compile(r"const views=\[(.*?)\];", re.S)
_UI_NAME = re.compile(r"\['([a-z0-9-]+)'")
_BOT_TUPLE = re.compile(r'\("([a-z_]+)",\s*"[^"]+"\)')
_BOT_COMMANDS_HEADER = "BOT_COMMANDS: tuple[tuple[str, str], ...] = ("
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class AcceptanceError(RuntimeError):
    """Harness/oracle failure. Never a product PASS."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not nonempty)
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(value) == len(set(value))
    )


def _case_timeout_ns(case: Mapping[str, Any]) -> int:
    seconds = case.get("timeout_s")
    minimum = 0 if case.get("executable") is False else 1
    if type(seconds) is not int or not minimum <= seconds <= 86_400:
        raise AcceptanceError(f"matrix_case_timeout_invalid:{case.get('id')}")
    return seconds * 1_000_000_000


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or b"\0" in raw:
        raise AcceptanceError("matrix_unreadable")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("matrix_json_invalid") from exc
    if not isinstance(value, dict):
        raise AcceptanceError("matrix_not_object")
    if value.get("schema") != MATRIX_SCHEMA or value.get("schema_version") != 1:
        raise AcceptanceError("matrix_schema_mismatch")
    if value.get("not_a_backlog") is not True:
        raise AcceptanceError("matrix_must_not_be_a_backlog")
    capabilities = value.get("capabilities")
    cases = value.get("cases")
    rules = value.get("surface_rules")
    if not isinstance(capabilities, list) or not capabilities:
        raise AcceptanceError("matrix_capabilities_empty")
    if not isinstance(cases, list) or not cases:
        raise AcceptanceError("matrix_cases_empty")
    if not isinstance(rules, list) or not rules:
        raise AcceptanceError("matrix_surface_rules_empty")
    ids = [item.get("id") for item in capabilities if isinstance(item, dict)]
    if len(ids) != len(capabilities) or not _text_list(ids, nonempty=True):
        raise AcceptanceError("matrix_capability_ids_invalid")
    case_ids = [item.get("id") for item in cases if isinstance(item, dict)]
    if len(case_ids) != len(cases) or not _text_list(case_ids, nonempty=True):
        raise AcceptanceError("matrix_case_ids_invalid")
    cap_set = set(ids)
    scope = value.get("release_scope")
    if not isinstance(scope, dict) or set(scope) != OBLIGATIONS:
        raise AcceptanceError("matrix_release_scope_invalid")
    if any(cap.get("obligation") not in OBLIGATIONS for cap in capabilities):
        raise AcceptanceError("matrix_capability_obligation_invalid")
    for obligation, members in scope.items():
        if not _text_list(members) or set(members) != {
            cap["id"] for cap in capabilities if cap["obligation"] == obligation
        }:
            raise AcceptanceError(f"matrix_release_scope_mismatch:{obligation}")
    patterns = [rule.get("pattern") for rule in rules if isinstance(rule, dict)]
    if len(patterns) != len(rules) or not _text_list(patterns, nonempty=True):
        raise AcceptanceError("matrix_surface_rule_ids_invalid")
    for case in cases:
        if not isinstance(case, dict):
            raise AcceptanceError("matrix_case_not_object")
        if case.get("capability_id") not in cap_set:
            raise AcceptanceError(f"matrix_case_unknown_capability:{case.get('id')}")
        if case.get("layer") not in LAYERS:
            raise AcceptanceError(f"matrix_case_layer_invalid:{case.get('id')}")
        if type(case.get("executable")) is not bool:
            raise AcceptanceError(f"matrix_case_executable_invalid:{case.get('id')}")
        _case_timeout_ns(case)
        driver = case.get("execution_driver")
        if (
            not isinstance(driver, str)
            or driver not in EXECUTION_DRIVERS
            or case["layer"] not in EXECUTION_DRIVERS[driver]
            or (driver == "unimplemented") == case["executable"]
        ):
            raise AcceptanceError(f"matrix_case_driver_invalid:{case.get('id')}")
        if type(case.get("release_required")) is not bool:
            raise AcceptanceError(f"matrix_case_release_flag_invalid:{case.get('id')}")
        if not _text_list(case.get("covered_surfaces")) or not set(case["covered_surfaces"]).issubset(
            patterns
        ):
            raise AcceptanceError(f"matrix_case_surfaces_invalid:{case['id']}")
        if not _text_list(case.get("node_ids"), nonempty=case["executable"]):
            raise AcceptanceError(f"matrix_case_nodes_invalid:{case['id']}")
        if case["executable"] and not isinstance(case.get("handler"), str):
            raise AcceptanceError(f"matrix_case_handler_invalid:{case['id']}")
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("pattern") or ""):
            raise AcceptanceError("matrix_surface_rule_invalid")
        if rule.get("capability_id") not in cap_set:
            raise AcceptanceError(f"matrix_rule_unknown_capability:{rule.get('pattern')}")
        if rule.get("obligation") not in OBLIGATIONS:
            raise AcceptanceError(f"matrix_rule_obligation_invalid:{rule.get('pattern')}")
        capability = capabilities[ids.index(rule["capability_id"])]
        if rule["obligation"] != capability["obligation"]:
            raise AcceptanceError(f"matrix_rule_obligation_mismatch:{rule['pattern']}")
        if not _text_list(rule.get("case_ids")) or not set(rule["case_ids"]).issubset(case_ids):
            raise AcceptanceError(f"matrix_rule_cases_invalid:{rule['pattern']}")
        if not _text_list(rule.get("required_layers"), nonempty=True) or not set(
            rule["required_layers"]
        ).issubset(LAYERS):
            raise AcceptanceError(f"matrix_rule_layers_invalid:{rule['pattern']}")
        expected_links = {case["id"] for case in cases if rule["pattern"] in case["covered_surfaces"]}
        if set(rule["case_ids"]) != expected_links:
            raise AcceptanceError(f"matrix_rule_case_link_mismatch:{rule['pattern']}")
    return value


def discover_telegram_commands(root: Path = ROOT) -> tuple[str, ...]:
    text = (root / "friday" / "telegram_bridge" / "_base.py").read_text(encoding="utf-8")
    start = text.find(_BOT_COMMANDS_HEADER)
    if start < 0:
        raise AcceptanceError("telegram_commands_unreadable")
    end = text.find("\n)\n", start)
    if end < 0:
        raise AcceptanceError("telegram_commands_unreadable")
    names = _BOT_TUPLE.findall(text[start:end])
    if "chat" not in names or "help" not in names:
        raise AcceptanceError("telegram_commands_unreadable")
    return tuple(f"telegram:{name}" for name in names)


def discover_ui_views(root: Path = ROOT) -> tuple[str, ...]:
    text = (root / "friday" / "admin_ui" / "static" / "app.js").read_text(encoding="utf-8")
    match = _UI_VIEWS.search(text)
    if match is None:
        raise AcceptanceError("admin_ui_views_unreadable")
    names = _UI_NAME.findall(match.group(1))
    if "dashboard" not in names or "inbox" not in names:
        raise AcceptanceError("admin_ui_views_incomplete")
    return tuple(f"ui:{name}" for name in names)


def discover_cli_commands(root: Path = ROOT) -> tuple[str, ...]:
    text = (root / "friday" / "cli.py").read_text(encoding="utf-8")
    names = _CLI_PARSER.findall(text)
    if "backup" not in names or "server" not in names:
        raise AcceptanceError("cli_commands_unreadable")
    # Preserve first-seen order, drop duplicates from help re-binds.
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(f"cli:{name}")
    return tuple(ordered)


def _source_admin_router_fallback(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root / "friday" / "admin_api")
    except ValueError:
        return ""
    if relative.as_posix() != "__init__.py":
        return "/api/admin"
    return ""


def _source_router_prefix(path: Path, call: ast.Call, root: Path) -> str:
    if any(keyword.arg is None for keyword in call.keywords):
        relative = path.relative_to(root).as_posix()
        raise AcceptanceError(f"api_source_router_prefix_dynamic:{relative}:{call.lineno}")
    prefix_node = (
        call.args[0]
        if call.args
        else next((keyword.value for keyword in call.keywords if keyword.arg == "prefix"), None)
    )
    if prefix_node is None:
        return _source_admin_router_fallback(path, root)
    if not isinstance(prefix_node, ast.Constant) or not isinstance(prefix_node.value, str):
        relative = path.relative_to(root).as_posix()
        raise AcceptanceError(f"api_source_router_prefix_dynamic:{relative}:{call.lineno}")
    prefix = prefix_node.value
    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        relative = path.relative_to(root).as_posix()
        raise AcceptanceError(f"api_source_router_prefix_invalid:{relative}:{call.lineno}")
    return prefix


_SourceReceiverBinding = tuple[str, str] | None
_SOURCE_BINDING_UNTRACKED = object()


def _source_target_names(target: ast.AST | None) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(name for item in target.elts for name in _source_target_names(item))
    if isinstance(target, ast.Starred):
        return _source_target_names(target.value)
    return ()


def _source_legacy_binding(receiver: str, path: Path, root: Path) -> tuple[str, str]:
    if receiver in {"router", "admin_router"}:
        return ("router", _source_admin_router_fallback(path, root))
    return ("application", "")


def _source_value_binding(
    value: ast.expr | None,
    bindings: Mapping[str, _SourceReceiverBinding],
    path: Path,
    root: Path,
) -> tuple[str, str] | None | object:
    if isinstance(value, ast.Call):
        function = value.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else (function.attr if isinstance(function, ast.Attribute) else "")
        )
        if name == "APIRouter":
            return ("router", _source_router_prefix(path, value, root))
        if name in {"FastAPI", "Starlette"}:
            return ("application", "")
    if isinstance(value, ast.Name):
        if value.id in bindings:
            return bindings[value.id]
        if value.id in _LEGACY_API_DECORATOR_RECEIVERS:
            return _source_legacy_binding(value.id, path, root)
    return _SOURCE_BINDING_UNTRACKED


def _source_mark_ambiguous(
    bindings: dict[str, _SourceReceiverBinding],
    names: Sequence[str],
) -> tuple[str, ...]:
    affected: list[str] = []
    for name in names:
        if name in bindings or name in _LEGACY_API_DECORATOR_RECEIVERS:
            bindings[name] = None
            affected.append(name)
    return tuple(affected)


def _source_function_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    arguments = node.args
    names = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    if arguments.vararg is not None:
        names.append(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.append(arguments.kwarg.arg)
    return tuple(names)


def _source_expression_bound_names(
    expression: ast.AST | None,
    expression_cache: dict[ast.AST, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    if expression is None:
        return ()
    if expression_cache is None:
        expression_cache = {}
    cached = expression_cache.get(expression)
    if cached is not None:
        return cached
    names: list[str] = []
    children: tuple[ast.AST, ...]
    if isinstance(expression, ast.NamedExpr):
        names.extend(_source_target_names(expression.target))
        children = (expression.value,)
    elif isinstance(expression, ast.Lambda):
        # Defaults execute when a lambda is created; its body is deferred.
        children = tuple(
            default
            for default in (*expression.args.defaults, *expression.args.kw_defaults)
            if default is not None
        )
    else:
        children = tuple(ast.iter_child_nodes(expression))
    for child in children:
        names.extend(_source_expression_bound_names(child, expression_cache))
    result = tuple(names)
    expression_cache[expression] = result
    return result


def _source_definition_binding_expressions(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[ast.AST, ...]:
    expressions: list[ast.AST] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            if argument.annotation is not None:
                expressions.append(argument.annotation)
        for argument in (arguments.vararg, arguments.kwarg):
            if argument is not None and argument.annotation is not None:
                expressions.append(argument.annotation)
        expressions.extend(arguments.defaults)
        expressions.extend(default for default in arguments.kw_defaults if default is not None)
        if node.returns is not None:
            expressions.append(node.returns)
    else:
        expressions.extend(node.bases)
        expressions.extend(keyword.value for keyword in node.keywords)
    expressions.extend(getattr(node, "type_params", ()))
    return tuple(expressions)


def _source_loop_control_transfer_line(body: Sequence[ast.stmt]) -> int | None:
    class LoopControlVisitor(ast.NodeVisitor):
        line: int | None = None

        def _record(self, node: ast.Break | ast.Continue) -> None:
            if self.line is None:
                self.line = node.lineno

        def _skip(self, node: ast.AST) -> None:
            return

        def _nested_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
            # The nested body's transfers belong to it; its else runs outside that loop.
            for statement in node.orelse:
                self.visit(statement)

        visit_Break = visit_Continue = _record  # type: ignore[assignment]
        visit_For = visit_AsyncFor = visit_While = _nested_loop  # type: ignore[assignment]
        visit_FunctionDef = visit_AsyncFunctionDef = _skip  # type: ignore[assignment]
        visit_ClassDef = visit_Lambda = _skip  # type: ignore[assignment]

    visitor = LoopControlVisitor()
    for statement in body:
        visitor.visit(statement)
        if visitor.line is not None:
            return visitor.line
    return None


def _source_scope_bound_names(
    body: Sequence[ast.stmt],
    expression_cache: dict[ast.AST, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    if expression_cache is None:
        expression_cache = {}
    bound: set[str] = set()
    external: set[str] = set()

    def visit(statements: Sequence[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    bound.update(_source_target_names(target))
                bound.update(_source_expression_bound_names(node.value, expression_cache))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                bound.update(_source_target_names(node.target))
                bound.update(_source_expression_bound_names(node.value, expression_cache))
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    bound.update(_source_target_names(target))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add(
                        alias.asname
                        or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                external.update(node.names)
            elif isinstance(node, ast.If):
                bound.update(_source_expression_bound_names(node.test, expression_cache))
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                bound.update(_source_target_names(node.target))
                bound.update(_source_expression_bound_names(node.iter, expression_cache))
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.While):
                bound.update(_source_expression_bound_names(node.test, expression_cache))
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    bound.update(_source_expression_bound_names(item.context_expr, expression_cache))
                    bound.update(_source_target_names(item.optional_vars))
                visit(node.body)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                visit(node.body)
                for handler in node.handlers:
                    if handler.name is not None:
                        bound.add(handler.name)
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)
            elif isinstance(node, ast.Match):
                bound.update(_source_expression_bound_names(node.subject, expression_cache))
                for case in node.cases:
                    for pattern in ast.walk(case.pattern):
                        if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name is not None:
                            bound.add(pattern.name)
                        elif isinstance(pattern, ast.MatchMapping) and pattern.rest is not None:
                            bound.add(pattern.rest)
                    bound.update(_source_expression_bound_names(case.guard, expression_cache))
                    visit(case.body)
            else:
                bound.update(_source_expression_bound_names(node, expression_cache))

    visit(body)
    return tuple(sorted(bound - external))


def _source_merge_bindings(
    states: Sequence[Mapping[str, _SourceReceiverBinding]],
) -> dict[str, _SourceReceiverBinding]:
    merged: dict[str, _SourceReceiverBinding] = {}
    names = set().union(*(state.keys() for state in states))
    for name in names:
        values = [state.get(name, _SOURCE_BINDING_UNTRACKED) for state in states]
        first = values[0]
        if all(value == first for value in values[1:]):
            if first is not _SOURCE_BINDING_UNTRACKED:
                merged[name] = first  # type: ignore[assignment]
        else:
            merged[name] = None
    return merged


def _source_decorator_surface(
    decorator: ast.expr,
    bindings: Mapping[str, _SourceReceiverBinding],
    path: Path,
    root: Path,
) -> str | None:
    if (
        not isinstance(decorator, ast.Call)
        or not isinstance(decorator.func, ast.Attribute)
        or decorator.func.attr not in _API_DECORATOR_METHODS
        or not isinstance(decorator.func.value, ast.Name)
    ):
        return None
    receiver = decorator.func.value.id
    if receiver in bindings:
        binding = bindings[receiver]
        if binding is None:
            relative = path.relative_to(root).as_posix()
            raise AcceptanceError(f"api_source_router_binding_ambiguous:{relative}:{decorator.lineno}")
    elif receiver in _LEGACY_API_DECORATOR_RECEIVERS:
        binding = _source_legacy_binding(receiver, path, root)
    else:
        return None
    route_node = (
        decorator.args[0]
        if decorator.args
        else next((keyword.value for keyword in decorator.keywords if keyword.arg == "path"), None)
    )
    relative = path.relative_to(root).as_posix()
    if not isinstance(route_node, ast.Constant) or not isinstance(route_node.value, str):
        raise AcceptanceError(f"api_source_route_dynamic:{relative}:{decorator.lineno}")
    route = route_node.value
    if binding[0] == "router":
        prefix = binding[1].rstrip("/")
        if not prefix and not route:
            raise AcceptanceError(f"api_source_router_prefix_unknown:{relative}:{decorator.lineno}")
        if not route:
            joined = prefix
        elif route.startswith("/"):
            joined = f"{prefix}{route}" if prefix else route
        else:
            joined = f"{prefix}/{route}" if prefix else f"/{route}"
    else:
        joined = route if route.startswith("/") else f"/{route}"
    if not joined.startswith("/"):
        raise AcceptanceError(f"api_source_route_invalid:{relative}:{decorator.lineno}")
    return f"api:{decorator.func.attr.upper()} {joined}"


def _source_scan_body(
    body: Sequence[ast.stmt],
    inherited_bindings: Mapping[str, _SourceReceiverBinding],
    path: Path,
    root: Path,
    expression_cache: dict[ast.AST, tuple[str, ...]] | None = None,
) -> tuple[tuple[str, ...], dict[str, _SourceReceiverBinding], frozenset[str]]:
    # The parsed module is private and unchanged throughout this scan. Cache only
    # syntax-derived names, never binding-dependent route or ambiguity decisions.
    # A new top-level scan receives a fresh cache, including after AST/file edits.
    if expression_cache is None:
        expression_cache = {}
    bindings = dict(inherited_bindings)
    local_known: dict[str, tuple[str, str]] = {}
    found: list[str] = []
    mutated: set[str] = set()

    def replace_state(
        state: dict[str, _SourceReceiverBinding],
        child_mutations: Sequence[str] = (),
    ) -> None:
        changed = {
            name
            for name in set(bindings) | set(state)
            if bindings.get(name, _SOURCE_BINDING_UNTRACKED) != state.get(name, _SOURCE_BINDING_UNTRACKED)
        }
        mutated.update(child_mutations)
        mutated.update(changed)
        bindings.clear()
        bindings.update(state)
        for name in changed:
            local_known.pop(name, None)

    def apply_assignment(node: ast.Assign | ast.AnnAssign) -> None:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        else:
            targets = [node.target]
            value = node.value
        names = tuple(name for target in targets for name in _source_target_names(target))
        simple = bool(names) and all(isinstance(target, ast.Name) for target in targets)
        expression_names = _source_expression_bound_names(value, expression_cache)
        expression_mutations = _source_mark_ambiguous(bindings, expression_names)
        mutated.update(expression_mutations)
        for name in expression_mutations:
            local_known.pop(name, None)
        value_binding = _source_value_binding(value, bindings, path, root)
        if simple and value_binding is not _SOURCE_BINDING_UNTRACKED:
            for name in names:
                if value_binding is None:
                    bindings[name] = None
                    mutated.add(name)
                    local_known.pop(name, None)
                    continue
                previous = local_known.get(name)
                if previous is not None and previous != value_binding:
                    relative = path.relative_to(root).as_posix()
                    raise AcceptanceError(f"api_source_router_rebound:{relative}:{node.lineno}")
                bindings[name] = value_binding
                mutated.add(name)
                local_known[name] = value_binding
            return
        mutated.update(_source_mark_ambiguous(bindings, names))
        for name in names:
            if bindings.get(name) is None:
                local_known.pop(name, None)

    def scan_loop_body(
        loop_body: Sequence[ast.stmt],
        entry: Mapping[str, _SourceReceiverBinding],
    ) -> tuple[tuple[str, ...], dict[str, _SourceReceiverBinding], frozenset[str]]:
        first_found, first_state, first_mutations = _source_scan_body(
            loop_body, entry, path, root, expression_cache
        )
        transfer_line = _source_loop_control_transfer_line(loop_body)
        receiver_names = {name for name, binding in entry.items() if binding is not None} | set(
            _LEGACY_API_DECORATOR_RECEIVERS
        )
        if transfer_line is not None and receiver_names.intersection(first_mutations):
            relative = path.relative_to(root).as_posix()
            raise AcceptanceError(f"api_source_router_binding_ambiguous:{relative}:{transfer_line}")
        carried = {
            name
            for name in set(entry) | set(first_state)
            if entry.get(name, _SOURCE_BINDING_UNTRACKED) != first_state.get(name, _SOURCE_BINDING_UNTRACKED)
        }
        if not carried:
            return first_found, first_state, first_mutations
        repeated_entry = dict(entry)
        for name in carried:
            repeated_entry[name] = None
        repeated_found, repeated_state, repeated_mutations = _source_scan_body(
            loop_body, repeated_entry, path, root, expression_cache
        )
        return (
            repeated_found,
            repeated_state,
            frozenset((*first_mutations, *repeated_mutations, *carried)),
        )

    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            apply_assignment(node)
            continue
        if isinstance(node, ast.AugAssign):
            names = _source_target_names(node.target)
            affected = _source_mark_ambiguous(
                bindings,
                (*names, *_source_expression_bound_names(node.value, expression_cache)),
            )
            mutated.update(affected)
            for name in affected:
                local_known.pop(name, None)
            continue
        if isinstance(node, ast.Delete):
            names = tuple(name for target in node.targets for name in _source_target_names(target))
            affected = _source_mark_ambiguous(bindings, names)
            mutated.update(affected)
            for name in affected:
                local_known.pop(name, None)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = tuple(
                alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                for alias in node.names
            )
            affected = _source_mark_ambiguous(bindings, names)
            mutated.update(affected)
            for name in affected:
                local_known.pop(name, None)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                surface = _source_decorator_surface(decorator, bindings, path, root)
                if surface is not None:
                    found.append(surface)
                affected = _source_mark_ambiguous(
                    bindings, _source_expression_bound_names(decorator, expression_cache)
                )
                mutated.update(affected)
                for name in affected:
                    local_known.pop(name, None)
            for expression in _source_definition_binding_expressions(node):
                affected = _source_mark_ambiguous(
                    bindings, _source_expression_bound_names(expression, expression_cache)
                )
                mutated.update(affected)
                for name in affected:
                    local_known.pop(name, None)
            child_bindings = dict(bindings)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                shadowed = (
                    *_source_function_parameters(node),
                    *_source_scope_bound_names(node.body, expression_cache),
                )
                _source_mark_ambiguous(child_bindings, shadowed)
            child_found, _, _ = _source_scan_body(node.body, child_bindings, path, root, expression_cache)
            found.extend(child_found)
            mutated.update(_source_mark_ambiguous(bindings, (node.name,)))
            local_known.pop(node.name, None)
            continue
        if isinstance(node, ast.If):
            test_mutations = _source_mark_ambiguous(
                bindings, _source_expression_bound_names(node.test, expression_cache)
            )
            mutated.update(test_mutations)
            body_found, body_state, body_mutations = _source_scan_body(
                node.body, bindings, path, root, expression_cache
            )
            else_found, else_state, else_mutations = _source_scan_body(
                node.orelse, bindings, path, root, expression_cache
            )
            found.extend((*body_found, *else_found))
            replace_state(
                _source_merge_bindings((body_state, else_state)),
                (*body_mutations, *else_mutations),
            )
            continue
        if isinstance(node, (ast.For, ast.AsyncFor)):
            iter_mutations = _source_mark_ambiguous(
                bindings, _source_expression_bound_names(node.iter, expression_cache)
            )
            mutated.update(iter_mutations)
            body_start = dict(bindings)
            target_mutations = _source_mark_ambiguous(body_start, _source_target_names(node.target))
            body_found, body_state, body_mutations = scan_loop_body(node.body, body_start)
            loop_state = _source_merge_bindings((bindings, body_state))
            else_found, else_state, else_mutations = _source_scan_body(
                node.orelse, loop_state, path, root, expression_cache
            )
            found.extend((*body_found, *else_found))
            replace_state(
                else_state,
                (*target_mutations, *body_mutations, *else_mutations),
            )
            continue
        if isinstance(node, ast.While):
            test_mutations = _source_mark_ambiguous(
                bindings, _source_expression_bound_names(node.test, expression_cache)
            )
            mutated.update(test_mutations)
            body_found, body_state, body_mutations = scan_loop_body(node.body, bindings)
            loop_state = _source_merge_bindings((bindings, body_state))
            else_found, else_state, else_mutations = _source_scan_body(
                node.orelse, loop_state, path, root, expression_cache
            )
            found.extend((*body_found, *else_found))
            replace_state(else_state, (*body_mutations, *else_mutations))
            continue
        if isinstance(node, (ast.With, ast.AsyncWith)):
            child = dict(bindings)
            with_mutations: set[str] = set()
            for item in node.items:
                with_mutations.update(
                    _source_mark_ambiguous(
                        child, _source_expression_bound_names(item.context_expr, expression_cache)
                    )
                )
                with_mutations.update(_source_mark_ambiguous(child, _source_target_names(item.optional_vars)))
            child_found, child_state, child_mutations = _source_scan_body(
                node.body, child, path, root, expression_cache
            )
            found.extend(child_found)
            replace_state(child_state, (*with_mutations, *child_mutations))
            continue
        if isinstance(node, (ast.Try, ast.TryStar)):
            body_found, body_state, body_mutations = _source_scan_body(
                node.body, bindings, path, root, expression_cache
            )
            else_found, normal_state, else_mutations = _source_scan_body(
                node.orelse, body_state, path, root, expression_cache
            )
            found.extend((*body_found, *else_found))
            exits = [normal_state]
            handler_mutations: set[str] = set()
            for handler in node.handlers:
                handler_start = dict(bindings)
                for name in body_mutations:
                    handler_start[name] = None
                if handler.name is not None:
                    handler_start[handler.name] = None
                    handler_mutations.add(handler.name)
                handler_found, handler_state, branch_mutations = _source_scan_body(
                    handler.body, handler_start, path, root, expression_cache
                )
                found.extend(handler_found)
                exits.append(handler_state)
                handler_mutations.update(branch_mutations)
            merged = _source_merge_bindings(exits)
            branch_mutations = {
                *body_mutations,
                *else_mutations,
                *handler_mutations,
            }
            if node.finalbody:
                safety_start = dict(merged)
                for name in branch_mutations:
                    safety_start[name] = None
                final_found, _, safety_mutations = _source_scan_body(
                    node.finalbody, safety_start, path, root, expression_cache
                )
                _, final_state, final_mutations = _source_scan_body(
                    node.finalbody, merged, path, root, expression_cache
                )
                found.extend(final_found)
                replace_state(
                    final_state,
                    (
                        *branch_mutations,
                        *safety_mutations,
                        *final_mutations,
                    ),
                )
            else:
                replace_state(merged, tuple(branch_mutations))
            continue
        if isinstance(node, ast.Match):
            subject_mutations = _source_mark_ambiguous(
                bindings, _source_expression_bound_names(node.subject, expression_cache)
            )
            mutated.update(subject_mutations)
            exits: list[Mapping[str, _SourceReceiverBinding]] = [dict(bindings)]
            case_mutations: set[str] = set()
            for case in node.cases:
                case_start = dict(bindings)
                pattern_names: list[str] = []
                for pattern in ast.walk(case.pattern):
                    if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name is not None:
                        pattern_names.append(pattern.name)
                    elif isinstance(pattern, ast.MatchMapping) and pattern.rest is not None:
                        pattern_names.append(pattern.rest)
                case_mutations.update(_source_mark_ambiguous(case_start, pattern_names))
                case_mutations.update(
                    _source_mark_ambiguous(
                        case_start, _source_expression_bound_names(case.guard, expression_cache)
                    )
                )
                case_found, case_state, branch_mutations = _source_scan_body(
                    case.body, case_start, path, root, expression_cache
                )
                found.extend(case_found)
                exits.append(case_state)
                case_mutations.update(branch_mutations)
            replace_state(_source_merge_bindings(exits), tuple(case_mutations))
            continue
        affected = _source_mark_ambiguous(bindings, _source_expression_bound_names(node, expression_cache))
        mutated.update(affected)
        for name in affected:
            local_known.pop(name, None)
    return (tuple(found), bindings, frozenset(mutated))


def _source_scope_surfaces(
    body: Sequence[ast.stmt],
    inherited_bindings: Mapping[str, _SourceReceiverBinding],
    path: Path,
    root: Path,
) -> tuple[str, ...]:
    found, _, _ = _source_scan_body(body, inherited_bindings, path, root)
    return found


def discover_api_from_source(root: Path = ROOT) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for path in sorted((root / "friday").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as error:
            relative = path.relative_to(root).as_posix()
            raise AcceptanceError(f"api_source_parse_failed:{relative}") from error
        for surface in _source_scope_surfaces(tree.body, {}, path, root):
            if surface not in seen:
                seen.add(surface)
                found.append(surface)
    if len(found) < 40:
        raise AcceptanceError("api_source_scan_too_small")
    return tuple(found)


def _schema_api_surfaces(application: Any) -> tuple[str, ...]:
    schema = application.openapi()
    items = [
        f"api:{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in _HTTP_METHODS
    ]
    if len(items) < 100:
        raise AcceptanceError("openapi_surface_too_small")
    return tuple(sorted(items))


def discover_api_from_openapi(settings: Any) -> tuple[str, ...]:
    from friday.server import create_app

    return _schema_api_surfaces(create_app(settings))


def discover_api_from_runtime(settings: Any) -> tuple[str, ...]:
    from fastapi.routing import iter_route_contexts
    from starlette.routing import Mount, Route, WebSocketRoute

    from friday.server import create_app

    # Construction registers routers; no lifespan, DB, workers or requests run.
    application = create_app(settings)
    items = set(_schema_api_surfaces(application))
    visited = 0

    def visit(routes: Any, prefix: str = "", depth: int = 0) -> None:
        nonlocal visited
        if depth > 16:
            raise AcceptanceError("runtime_surface_nesting_exceeded")
        # FastAPI's route contexts retain effective include-router prefixes;
        # reading original APIRouter paths would lose dynamic mount identity.
        for context in iter_route_contexts(routes):
            route = context.original_route
            effective = getattr(context, "starlette_route", None) or context
            visited += 1
            if visited > 10000:
                raise AcceptanceError("runtime_surface_count_exceeded")
            path = effective.path
            if not isinstance(path, str) or not path.startswith("/"):
                raise AcceptanceError("runtime_surface_route_kind_unknown")
            full = prefix + path
            if isinstance(route, Mount):
                # Record the mount even if its ASGI application has no routes
                # (for example StaticFiles). Never silently skip that entrance.
                items.add(f"api:MOUNT {full}")
                visit(route.routes, full, depth + 1)
            elif isinstance(route, WebSocketRoute):
                items.add(f"api:WEBSOCKET {full}")
            elif isinstance(route, Route):
                methods = effective.methods
                if not methods or not set(methods).issubset(_HTTP_METHODS):
                    raise AcceptanceError("runtime_surface_method_unknown")
                items.update(f"api:{method} {full}" for method in methods)
            else:
                raise AcceptanceError("runtime_surface_route_kind_unknown")

    visit(application.routes)
    return tuple(sorted(items))


def _runtime_discovery_worker() -> int:
    from tools import synthetic_live_battery as battery

    sink = battery._BoundedTextSink(battery.MAX_WORKER_LOG_BYTES)
    try:
        with (
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
            battery.LocalEndpointNetworkGuard(()) as network,
        ):
            battery._reject_preloaded_product_modules()
            import friday
            from friday.config import load_settings

            if Path(friday.__file__).resolve().parent != ROOT / "friday":
                raise AcceptanceError("runtime_surface_source_authority_invalid")
            settings = load_settings()
            api = discover_api_from_runtime(settings)
            if network.denied_attempts or sink.truncated:
                raise AcceptanceError("runtime_surface_discovery_boundary_failed")
        report = {
            "schema": "friday.r10-surface-discovery.v1",
            "root_sha256": hashlib.sha256(str(ROOT).encode()).hexdigest(),
            "api": api,
            "network_denied": network.denied_attempts,
        }
    except Exception:
        _print({"error": "runtime_surface_discovery_failed"})
        return 2
    _print(report)
    return 0


def _isolated_runtime_surfaces(root: Path) -> tuple[str, ...]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools import quality_gate as gate
    from tools import synthetic_live_battery as battery

    root = root.resolve()
    if root != ROOT:
        raise AcceptanceError("runtime_surface_source_authority_invalid")
    bootstrap = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from tools import release_1_0_acceptance as a;"
        "raise SystemExit(a._runtime_discovery_worker())"
    )
    try:
        with gate._isolated_test_environment(prepare_schema_backups=False, source_root=root) as isolated:
            # Reuse the canonical empty env/private home, without ambient provider
            # credentials, proxy settings, installed-site selectors or test assets.
            names = {
                "FRIDAY_HOME",
                "JERICHO_HOME",
                "FRIDAY_ENV_FILE",
                "JERICHO_ENV_FILE",
                "FRIDAY_DATABASE_PATH",
                "JERICHO_DATABASE_PATH",
                "FRIDAY_DATABASE_MUST_EXIST",
                "JERICHO_DATABASE_MUST_EXIST",
                "FRIDAY_LLM_ENABLED",
                "FRIDAY_EMBEDDINGS_ENABLED",
                "FRIDAY_WORKERS_ENABLED",
                "FRIDAY_CODE_EXECUTION_ENABLED",
                "TMPDIR",
                "XDG_CONFIG_HOME",
            }
            environment = {key: isolated[key] for key in names}
            environment.update(
                PATH="/usr/bin:/bin",
                LANG="C.UTF-8",
                HOME=isolated["FRIDAY_HOME"],
                FRIDAY_SECONDARY_LLM_ENABLED="0",
            )
            outcome = battery._run_worker_bounded(
                [sys.executable, "-I", "-B", "-c", bootstrap, str(root)],
                cwd=Path(isolated["FRIDAY_HOME"]),
                env=environment,
                input_bytes=b"",
                timeout=30,
            )
    except Exception:
        raise AcceptanceError("runtime_surface_discovery_failed") from None
    if outcome.returncode or outcome.timed_out or outcome.stdout_truncated or outcome.stderr_truncated:
        raise AcceptanceError("runtime_surface_discovery_failed")
    try:
        report = json.loads(outcome.stdout)
    except (ValueError, UnicodeError):
        raise AcceptanceError("runtime_surface_discovery_invalid") from None
    if (
        not isinstance(report, dict)
        or set(report) != {"schema", "root_sha256", "api", "network_denied"}
        or report["schema"] != "friday.r10-surface-discovery.v1"
        or report["root_sha256"] != hashlib.sha256(str(root).encode()).hexdigest()
        or type(report["network_denied"]) is not int
        or report["network_denied"] != 0
        or not _text_list(report["api"], nonempty=True)
        or len(report["api"]) < 100
        or any(
            re.fullmatch(
                r"api:(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|MOUNT|WEBSOCKET) /[^\r\n\x00]*", item
            )
            is None
            for item in report["api"]
        )
    ):
        raise AcceptanceError("runtime_surface_discovery_invalid")
    return tuple(sorted(report["api"]))


def discover_surfaces(*, settings: Any | None = None, root: Path = ROOT) -> dict[str, tuple[str, ...]]:
    source = discover_api_from_source(root)
    runtime = (
        discover_api_from_runtime(settings) if settings is not None else _isolated_runtime_surfaces(root)
    )
    api = tuple(sorted(set(source) | set(runtime)))
    return {
        "api": api,
        "telegram": discover_telegram_commands(root),
        "ui": discover_ui_views(root),
        "cli": discover_cli_commands(root),
    }


def _classify_one(surface: str, rules: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    # The registry pins discovered method/path or command identities. A prefix
    # match would silently authorize a new surface without new evidence.
    return next((rule for rule in rules if rule.get("pattern") == surface), None)


def classify_surfaces(
    surfaces: Mapping[str, Sequence[str]],
    matrix: Mapping[str, Any],
    *,
    verified_case_ids: Sequence[str] = (),
    executed_case_layers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    rules = matrix["surface_rules"]
    classified: list[dict[str, Any]] = []
    unknown: list[str] = []
    for group in surfaces.values():
        for surface in group:
            rule = _classify_one(surface, rules)
            if rule is None:
                unknown.append(surface)
                continue
            classified.append(
                {
                    "surface": surface,
                    "capability_id": rule["capability_id"],
                    "obligation": rule["obligation"],
                }
            )
    # A case label alone is not evidence: it must actually cover this exact
    # surface and execute in every required layer.
    cases = {case["id"]: case for case in matrix["cases"]}
    verified = set(verified_case_ids)
    uncovered = []
    for row in classified:
        rule = _classify_one(row["surface"], rules)
        links = rule.get("case_ids", []) if rule else []
        required_layers = rule.get("required_layers", ["deterministic"]) if rule else []
        linked_layers = {
            cases[case_id]["layer"]
            for case_id in links
            if case_id in verified
            and case_id in cases
            and cases[case_id].get("executable") is True
            and row["surface"] in cases[case_id].get("covered_surfaces", [])
            # Collection can establish deterministic test bindings. Native,
            # browser and device execution require bound run receipts; the
            # model-free Word protocol node cannot satisfy that live layer.
            and (
                cases[case_id]["layer"] == "deterministic"
                if executed_case_layers is None
                else executed_case_layers.get(case_id) == cases[case_id]["layer"]
            )
        }
        if not links or not required_layers or not set(required_layers).issubset(linked_layers):
            uncovered.append(row["surface"])
    required_unknown = unknown[:]
    return {
        "classified": len(classified),
        "unknown": unknown,
        "unknown_count": len(unknown),
        "required_gap": required_unknown,
        "coverage_gaps": uncovered,
        "by_obligation": _count_by(classified, "obligation"),
        "by_capability": _count_by(classified, "capability_id"),
        "rows": classified,
        "coverage_kind": "structural" if executed_case_layers is None else "executed",
    }


def _pytest_source_bindings_exist(nodeids: Sequence[str]) -> bool:
    from tools.quality_gate_inventory import InventoryError, function_id

    parsed: dict[Path, ast.Module] = {}
    try:
        for nodeid in nodeids:
            module, *qualified = function_id(nodeid).split("::")
            path = ROOT / module
            if path not in parsed:
                details = path.lstat()
                if (
                    path.resolve(strict=True) != path
                    or not stat.S_ISREG(details.st_mode)
                    or details.st_nlink != 1
                ):
                    return False
                parsed[path] = ast.parse(path.read_text(encoding="utf-8"))
            body = parsed[path].body
            for index, name in enumerate(qualified):
                kinds = (
                    (ast.FunctionDef, ast.AsyncFunctionDef)
                    if index == len(qualified) - 1
                    else (ast.ClassDef,)
                )
                matches = [node for node in body if isinstance(node, kinds) and node.name == name]
                if len(matches) != 1:
                    return False
                body = matches[0].body
        return bool(nodeids)
    except (OSError, UnicodeError, SyntaxError, InventoryError):
        return False


def registered_case_handlers() -> dict[str, tuple[Any, str, tuple[str, ...], str]]:
    # Import the same canonical module from scripts and pytest. The returned
    # callables are the actual dispatch registry, not names copied from matrix.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools import quality_gate as gate
    from tools import release_1_0_app_soak as app_soak
    from tools import release_1_0_live_journeys as journeys
    from tools import release_1_0_native as native
    from tools import release_1_0_telegram_roundtrip as telegram_roundtrip

    test = "tests/test_release_1_0_journeys.py::test_additional_deterministic_journeys_cover_the_required_user_paths"
    handlers = {
        case_id: (handler, "deterministic", (f"{test}[{case_id}]",), "journey")
        for case_id, handler in journeys.RUNNERS.items()
    }
    live_test = "tests/test_release_1_0_live_cases.py::test_named_source_word_oracle_uses_real_owned_upload_download_and_history"
    handlers.update(
        {
            case_id: (handler, "isolated-live", (f"{live_test}[{case_id}]",), "native")
            for case_id, handler in native.live_handlers().items()
        }
    )
    for case_id in (
        "R10-NEG-CTRL-WRONG-DIGEST",
        "R10-NEG-CTRL-EMPTY-COLLECTION",
        "R10-NEG-CTRL-FOREIGN-CANARY",
    ):
        handlers[case_id] = (
            negative_controls,
            "harness",
            ("tests/test_release_1_0_acceptance.py::test_negative_controls_turn_the_oracle_red",),
            "harness",
        )
    handlers.update(
        {
            case_id: (gate.execute_tier, layer, nodes, "canonical-pytest")
            for case_id, (layer, nodes, _surfaces) in PYTEST_CASE_BINDINGS.items()
        }
    )
    handlers["R10-LIVE-APP-SOAK"] = (
        app_soak.run_actual_app_soak,
        "isolated-live",
        (
            "tests/test_release_1_0_app_soak_observed_controls.py::test_http_200_offline_without_reminder_effect_is_red_and_continues_only_identity_reads",
        ),
        "app-soak",
    )
    handlers["R10-LIVE-TELEGRAM-ROUNDTRIP"] = (
        telegram_roundtrip.run_roundtrip,
        "deployment-device",
        (
            "tests/test_release_1_0_telegram_receipts.py::test_synthetic_complete_reader_path_is_mechanics_only",
        ),
        "telegram-roundtrip",
    )
    return handlers


def audit_case_bindings(matrix: Mapping[str, Any], nodeids: Sequence[str] | None) -> dict[str, Any]:
    handlers = registered_case_handlers()
    collected = set(nodeids or ())
    complaints = []
    verified = []
    if not collected:
        complaints.append("case_collection_missing_or_empty")
    for case in matrix["cases"]:
        case_id = case["id"]
        if case.get("executable") is not True:
            continue
        failures = []
        entry = handlers.get(case_id)
        if entry is None or not callable(entry[0]):
            failures.append("executable_handler_missing")
        else:
            handler, layer, exact_nodes, driver = entry
            if case.get("execution_driver") != driver:
                failures.append("executable_driver_mismatch")
            origin = inspect.getsourcefile(handler)
            if origin is None:
                failures.append("executable_handler_origin_missing")
            else:
                try:
                    relative = Path(origin).resolve().relative_to(ROOT.resolve()).as_posix()
                except ValueError:
                    relative = "outside-candidate"
                if case.get("handler") != f"{relative}:{handler.__name__}":
                    failures.append("executable_handler_mismatch")
            if case.get("layer") != layer:
                failures.append("executable_layer_mismatch")
            if case.get("node_ids") != list(exact_nodes):
                failures.append("executable_nodes_mismatch")
            if driver == "canonical-pytest":
                if case.get("covered_surfaces") != list(PYTEST_CASE_BINDINGS[case_id][2]):
                    failures.append("executable_surfaces_mismatch")
                if not _pytest_source_bindings_exist(exact_nodes):
                    failures.append("executable_test_source_missing")
        nodes = case.get("node_ids")
        if (
            not isinstance(nodes, list)
            or not nodes
            or any(not isinstance(node, str) or node not in collected for node in nodes)
        ):
            failures.append("case_nodes_missing_or_uncollected")
        if failures:
            complaints.extend(f"{code}:{case_id}" for code in failures)
        else:
            verified.append(case_id)
    declared = {case["id"] for case in matrix["cases"]}
    complaints.extend(f"handler_case_undeclared:{case_id}" for case_id in handlers if case_id not in declared)
    return {
        "valid": not complaints,
        "complaints": sorted(complaints),
        "verified_case_ids": sorted(verified),
        "collected_nodes": len(collected),
    }


def _read_bound_receipt_bytes(
    path: Path, expected_sha256: str, *, max_bytes: int = 64 << 20, allow_empty: bool = False
) -> bytes:
    """Read stable private bytes against an externally supplied digest."""
    from tools import quality_gate as gate

    if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise AcceptanceError("gate_receipt_digest_invalid")
    if type(max_bytes) is not int or not 0 < max_bytes <= 64 << 20:
        raise AcceptanceError("gate_receipt_limit_invalid")
    limit = max_bytes
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise AcceptanceError("gate_receipt_file_invalid")
        before = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or not (0 if allow_empty else 1) <= opened.st_size <= limit
                or gate._stat_identity(before) != gate._stat_identity(opened)
            ):
                raise AcceptanceError("gate_receipt_file_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            if (
                len(raw) != opened.st_size
                or gate._stat_identity(os.fstat(fd)) != gate._stat_identity(opened)
                or gate._stat_identity(path.lstat()) != gate._stat_identity(opened)
            ):
                raise AcceptanceError("gate_receipt_file_invalid")
        finally:
            os.close(fd)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AcceptanceError("gate_receipt_file_invalid") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AcceptanceError("gate_receipt_digest_mismatch")
    return raw


def _read_bound_gate_receipt(
    path: Path, expected_sha256: str, *, max_bytes: int = 64 << 20
) -> dict[str, Any]:
    raw = _read_bound_receipt_bytes(path, expected_sha256, max_bytes=max_bytes)

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate key")
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if not isinstance(value, dict) or raw != canonical.encode():
            raise ValueError("noncanonical receipt")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AcceptanceError("gate_receipt_json_invalid") from exc
    return value


def audit_native_execution(
    matrix: Mapping[str, Any],
    *,
    receipt_path: Path,
    receipt_sha256: str,
    expected_identity: Mapping[str, str],
    expected_run_id: str,
    expected_case_ids: Sequence[str],
    expected_secondary: bool,
    expected_secondary_mode: str | None,
) -> dict[str, Any]:
    """Read bound native transport evidence without dispatching or granting GO.

    The caller supplies the identity, run and selection independently. Worker
    validation remains native's responsibility; controller cleanup and elapsed
    time are separately checked against its retained final receipt.
    """
    from datetime import datetime, timedelta
    from math import isfinite

    from tools import document_contour_live_battery as lifecycle
    from tools import release_1_0_native as native
    from tools.release_1_0_live_cases import MAX_ARTIFACT_BYTES, WORD_VARIANTS, word_fixture

    def require(condition: bool, code: str) -> None:
        if not condition:
            raise AcceptanceError("native_" + code)

    def same_json(left: Any, right: Any) -> bool:
        pending = [(left, right)]
        while pending:
            first, second = pending.pop()
            if type(first) is not type(second):
                return False
            if isinstance(first, dict):
                if first.keys() != second.keys():
                    return False
                pending.extend((value, second[key]) for key, value in first.items())
            elif isinstance(first, list):
                if len(first) != len(second):
                    return False
                pending.extend(zip(first, second, strict=True))
            elif first != second:
                return False
        return True

    def strict_json(raw: bytes) -> dict[str, Any]:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result = dict(items)
            if len(result) != len(items):
                raise ValueError("duplicate JSON key")
            return result

        def constant(_value: str) -> Any:
            raise ValueError("nonfinite JSON constant")

        def finite_float(value: str) -> float:
            number = float(value)
            if not isfinite(number):
                raise ValueError("nonfinite decoded JSON number")
            return number

        try:
            value = json.loads(
                raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AcceptanceError("native_receipt_json_invalid") from exc
        require(isinstance(value, dict), "receipt_json_invalid")
        return value

    def private_bytes(path: Path, digest: str, maximum: int, *, empty: bool = False) -> bytes:
        try:
            return _read_bound_receipt_bytes(path, digest, max_bytes=maximum, allow_empty=empty)
        except AcceptanceError as exc:
            raise AcceptanceError("native_bound_file_invalid") from exc

    identity_lengths = {
        "candidate_sha": 40,
        "candidate_tree": 40,
        "candidate_source_sha256": 64,
        "wheel_sha256": 64,
        "installed_site_sha256": 64,
        "suite_sha256": 64,
        "model_environment_sha256": 64,
    }
    require(
        isinstance(expected_identity, Mapping)
        and set(expected_identity) == set(identity_lengths)
        and all(
            isinstance(expected_identity[k], str)
            and re.fullmatch(rf"[0-9a-f]{{{n}}}", expected_identity[k]) is not None
            for k, n in identity_lengths.items()
        ),
        "expected_identity_invalid",
    )
    require(
        isinstance(expected_run_id, str) and re.fullmatch(r"[0-9a-f]{32}", expected_run_id) is not None,
        "expected_run_invalid",
    )
    require(
        type(expected_secondary) is bool
        and (expected_secondary_mode is None or type(expected_secondary_mode) is str)
        and (
            expected_secondary_mode in {"shadow", "assist"}
            if expected_secondary
            else expected_secondary_mode in {None, "disabled"}
        ),
        "expected_secondary_invalid",
    )
    require(
        isinstance(expected_case_ids, (list, tuple))
        and bool(expected_case_ids)
        and all(isinstance(cid, str) for cid in expected_case_ids),
        "expected_selection_invalid",
    )
    selected = list(expected_case_ids)
    require(len(selected) == len(set(selected)), "expected_selection_invalid")
    specs = {case["id"]: case for case in matrix["cases"]}
    require(len(specs) == len(matrix["cases"]), "matrix_cases_invalid")
    native_ids = {row[0] for row in WORD_VARIANTS}
    for cid in selected:
        spec = specs.get(cid, {})
        require(
            cid in native_ids
            and spec.get("layer") == "isolated-live"
            and spec.get("execution_driver") == "native"
            and spec.get("executable") is True
            and spec.get("handler") == "tools/release_1_0_live_cases.py:run_word_case"
            and type(spec.get("timeout_s")) is int
            and 0 < spec["timeout_s"] <= lifecycle.WORKER_TIMEOUT_SEC,
            "expected_selection_invalid",
        )
    root = receipt_path.parent
    require(receipt_path.name == "summary.json", "receipt_path_invalid")
    summary = strict_json(private_bytes(receipt_path, receipt_sha256, 64 << 20))
    require(
        set(summary)
        == {
            "schema",
            "run_id",
            "planned",
            "results",
            "status",
            "root_failure",
            "go_emitted",
            "required_denominator",
            "required_results",
            "scope",
            "evidence",
        }
        and summary["schema"] == native.SCHEMA
        and summary["run_id"] == expected_run_id
        and type(summary["planned"]) is int
        and summary["planned"] == len(selected)
        and summary["go_emitted"] is False
        and summary["scope"] == "selected isolated-live cases only; not complete release acceptance",
        "receipt_shape_invalid",
    )
    rows = summary["results"]
    require(
        isinstance(rows, list)
        and len(rows) == len(selected)
        and all(isinstance(row, dict) for row in rows)
        and [row.get("id") for row in rows] == selected,
        "receipt_selection_invalid",
    )
    root_failure = summary["root_failure"]
    if root_failure is not None:
        require(
            isinstance(root_failure, dict)
            and set(root_failure)
            <= {
                "id",
                "code",
                "root_class",
                "error_type",
                "signal_number",
            }
            and root_failure.get("id") == "native-run-root"
            and isinstance(root_failure.get("code"), str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", root_failure["code"]) is not None
            and type(root_failure.get("root_class")) is str
            and root_failure["root_class"] in {"harness", "environment"},
            "root_failure_invalid",
        )
    projection_keys = {
        "id",
        "status",
        "failure_codes",
        "attempt",
        "duration_ms",
        "root_class",
        "process_cleanup_clear",
        "root_ref",
    }
    for row in rows:
        codes = row.get("failure_codes")
        require(
            set(row) <= projection_keys
            and type(row.get("status")) is str
            and row["status"] in {"PASS", "FAIL", "NOT_RUN"}
            and type(row.get("attempt")) is int
            and row["attempt"] in {0, 1}
            and isinstance(codes, list)
            and all(
                isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) is not None
                for code in codes
            )
            and codes == sorted(set(codes)),
            "case_outcome_invalid",
        )
        require(
            (not codes and row.get("root_class") is None)
            if row["status"] == "PASS"
            else (
                bool(codes)
                and type(row.get("root_class")) is str
                and row["root_class"] in {"harness", "environment", "product"}
            ),
            "case_outcome_invalid",
        )
        if "duration_ms" in row:
            require(type(row["duration_ms"]) is int and row["duration_ms"] >= 0, "case_duration_invalid")
        if "process_cleanup_clear" in row:
            require(type(row["process_cleanup_clear"]) is bool, "case_cleanup_invalid")
    require(
        summary["status"]
        == ("PASS" if root_failure is None and all(row["status"] == "PASS" for row in rows) else "FAIL"),
        "summary_status_invalid",
    )
    required = [case for case in matrix["cases"] if case["release_required"]]
    outcomes = {row["id"]: row["status"] for row in rows}
    require(
        type(summary["required_denominator"]) is int
        and summary["required_denominator"] == len(required)
        and same_json(
            summary["required_results"],
            [
                {
                    "id": case["id"],
                    "required_layer": case["layer"],
                    "status": outcomes.get(case["id"], "NOT_RUN"),
                    "selected": case["id"] in selected,
                }
                for case in required
            ],
        ),
        "required_results_invalid",
    )
    index = summary["evidence"]
    require(
        isinstance(index, dict) and index.get("schema") == "friday.r10-native-evidence.v1",
        "evidence_shape_invalid",
    )
    if index == {"schema": "friday.r10-native-evidence.v1", "invalid": True}:
        require(summary["status"] == "FAIL" and root_failure is not None, "evidence_shape_invalid")
        return {
            "status": "FAIL",
            "case_layers": {},
            "case_statuses": {cid: outcomes.get(cid, "NOT_RUN") for cid in specs},
            "results": rows,
            "root_failure": root_failure,
            "evidence_valid": False,
            "go_emitted": False,
        }
    require(
        set(index) == {"schema", "selected_cases", "identity", "wheel", "cases"}
        and index["selected_cases"] == selected
        and isinstance(index["cases"], list)
        and len(index["cases"]) == len(selected),
        "evidence_selection_invalid",
    )

    def reference(value: Any, name: str, *, maximum: int = 1 << 20, empty: bool = False) -> bytes | None:
        if value is None:
            return None
        require(
            isinstance(value, dict)
            and set(value) == {"path", "sha256", "size_bytes"}
            and value.get("path") == name
            and type(value.get("size_bytes")) is int
            and (0 if empty else 1) <= value["size_bytes"] <= maximum,
            "evidence_reference_invalid",
        )
        path = Path(name)
        require(
            not path.is_absolute()
            and path.as_posix() == name
            and ".." not in path.parts
            and "." not in path.parts,
            "evidence_reference_invalid",
        )
        raw = private_bytes(root / path, value["sha256"], maximum, empty=empty)
        require(len(raw) == value["size_bytes"], "evidence_size_mismatch")
        return raw

    identity_raw = reference(index["identity"], "frozen-identity.json")
    if identity_raw is not None:
        require(same_json(strict_json(identity_raw), dict(expected_identity)), "frozen_identity_mismatch")
    wheel_ref = index["wheel"]
    if wheel_ref is not None:
        require(
            isinstance(wheel_ref, dict)
            and isinstance(wheel_ref.get("path"), str)
            and Path(wheel_ref["path"]).name == wheel_ref["path"]
            and wheel_ref["path"].endswith(".whl"),
            "wheel_reference_invalid",
        )
        reference(wheel_ref, wheel_ref["path"], maximum=64 << 20)
        require(wheel_ref["sha256"] == expected_identity["wheel_sha256"], "wheel_identity_mismatch")
    layers = {}
    for ordinal, (cid, row, entry) in enumerate(zip(selected, rows, index["cases"], strict=True), 1):
        require(
            isinstance(entry, dict)
            and set(entry)
            == {
                "id",
                "index",
                "receipt",
                "probe_request",
                "probe_response",
                "worker_request",
                "worker_response",
                "artifacts",
            }
            and entry["id"] == cid
            and type(entry["index"]) is int
            and entry["index"] == ordinal
            and isinstance(entry["artifacts"], list),
            "case_evidence_invalid",
        )
        clean = row.get("process_cleanup_clear") is True
        evidence = f"case-{ordinal:03d}/evidence"
        names = {
            "receipt": f"{evidence}/case-receipt.json" if clean else f"case-{ordinal:03d}-receipt.json",
            "probe_request": f"{evidence}/runtime-probe-request.json",
            "probe_response": f"case-{ordinal:03d}-runtime-probe-response.json",
            "worker_request": f"{evidence}/worker-request.json",
            "worker_response": f"case-{ordinal:03d}-worker-response.json",
        }
        if not clean:
            require(
                all(entry[key] is None for key in names if key != "receipt") and entry["artifacts"] == [],
                "uncertain_evidence_traversal",
            )
        raw_files = {
            key: reference(entry[key], name, empty=key.endswith("response") and row["status"] != "PASS")
            for key, name in names.items()
        }
        final = strict_json(raw_files["receipt"]) if raw_files["receipt"] is not None else None
        if final is not None:
            projection = {key: value for key, value in final.items() if key in projection_keys}
            interrupted = (
                row["status"] == "NOT_RUN"
                and row.get("root_ref") == "native-run-root"
                and root_failure is not None
                and root_failure["code"] == "native_controller_interrupted"
                and set(final)
                == {
                    "id",
                    "identity",
                    "status",
                    "attempt",
                    "failure_codes",
                    "root_class",
                    "process_cleanup_clear",
                    "duration_ms",
                    "go_emitted",
                }
                and final["id"] == cid
                and final["status"] == "NOT_RUN"
                and type(final["attempt"]) is int
                and final["attempt"] == row["attempt"] == 1
                and final["root_class"] == row["root_class"] == "environment"
                and final["process_cleanup_clear"] is False
                and type(final["duration_ms"]) is int
                and final["duration_ms"] >= 0
                and isinstance(final["failure_codes"], list)
                and all(
                    isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code)
                    for code in final["failure_codes"]
                )
                and "native_controller_interrupted" in final["failure_codes"]
            )
            require(
                (same_json(projection, row) or interrupted) and final.get("go_emitted") is False,
                "controller_summary_mismatch",
            )
            retained_identity = final.get("identity")
            require(
                isinstance(retained_identity, dict)
                and all(retained_identity.get(key) == expected_identity[key] for key in identity_lengths)
                and retained_identity.get("run_id") == expected_run_id
                and retained_identity.get("case_id") == cid,
                "case_identity_mismatch",
            )
        else:
            require(
                row["status"] == "NOT_RUN"
                and row.get("root_ref") == "native-run-root"
                and root_failure is not None,
                "controller_receipt_missing",
            )
        artifact_bytes = {}
        for artifact in entry["artifacts"]:
            require(
                isinstance(artifact, dict)
                and set(artifact) == {"kind", "path", "sha256", "size_bytes"}
                and type(artifact.get("kind")) is str
                and artifact["kind"] in {"fixture_sha256", "artifact_sha256"}
                and artifact["kind"] not in artifact_bytes,
                "artifact_reference_invalid",
            )
            require(
                isinstance(artifact.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is not None,
                "artifact_reference_invalid",
            )
            artifact_bytes[artifact["kind"]] = reference(
                {key: value for key, value in artifact.items() if key != "kind"},
                f"{evidence}/artifact-{artifact['sha256']}.bin",
                maximum=MAX_ARTIFACT_BYTES,
            )
        if row["status"] != "PASS":
            # Failed stdout can be empty or malformed: hash it as diagnostic
            # bytes, preserve the bound controller outcome, grant no credit.
            continue
        require(
            clean
            and row["attempt"] == 1
            and identity_raw is not None
            and wheel_ref is not None
            and final is not None
            and final.get("process_cleanup_clear") is True
            and all(raw is not None for raw in raw_files.values()),
            "pass_evidence_incomplete",
        )
        try:
            probe_request = native._validate_probe_request(strict_json(raw_files["probe_request"]))
            request = native._validate_request(strict_json(raw_files["worker_request"]))
            probe_result = strict_json(raw_files["probe_response"])
            worker = strict_json(raw_files["worker_response"])
        except (native.NativeError, KeyError, TypeError, ValueError) as exc:
            raise AcceptanceError("native_worker_request_invalid") from exc
        for request_identity in (probe_request, request):
            require(
                all(request_identity[key] == expected_identity[key] for key in identity_lengths)
                and request_identity["run_id"] == expected_run_id
                and request_identity["case_id"] == cid,
                "case_identity_mismatch",
            )
        require(
            same_json(probe_request["candidate_files"], request["candidate_files"])
            and same_json(probe_request.get("secondary_ca"), request.get("secondary_ca")),
            "probe_request_mismatch",
        )
        require(
            same_json(probe_result.get("identity"), probe_request)
            and same_json(worker.get("identity"), request),
            "case_identity_mismatch",
        )
        require(
            native._runtime_probe_result_valid(probe_result, probe_request)
            and request["expected_effective_runtime_sha256"] == probe_result["effective_runtime_sha256"]
            and request["expected_worker_environment_sha256"] == probe_request["worker_environment_sha256"]
            and request["runtime_probe_receipt_sha256"]
            == hashlib.sha256(raw_files["probe_response"]).hexdigest(),
            "runtime_probe_invalid",
        )
        # Guard JSON enum types before invoking native validators which use sets.
        # Malformed artifact data is rejected here; validator programming errors
        # are not hidden behind a broad exception handler.
        require(
            type(worker.get("status")) is str
            and (worker.get("root_class") is None or type(worker.get("root_class")) is str),
            "worker_result_invalid",
        )
        require(
            native._worker_result_valid(
                worker,
                request,
                expected_secondary=expected_secondary,
                expected_secondary_mode=expected_secondary_mode,
            )
            and worker["status"] == "PASS",
            "worker_result_invalid",
        )
        controller_only = {"started_at", "ended_at", "process_cleanup_clear", "go_emitted"}
        require(
            set(final) == set(worker) | controller_only
            and all(same_json(final[key], value) for key, value in worker.items() if key != "duration_ms"),
            "controller_worker_mismatch",
        )
        require(
            type(final.get("duration_ms")) is int
            and type(row.get("duration_ms")) is int
            and 0 <= worker["duration_ms"] <= final["duration_ms"] <= specs[cid]["timeout_s"] * 1000,
            "case_deadline_invalid",
        )
        try:
            started = datetime.fromisoformat(final["started_at"].replace("Z", "+00:00"))
            ended = datetime.fromisoformat(final["ended_at"].replace("Z", "+00:00"))
            require(started.utcoffset() == ended.utcoffset() == timedelta(0), "case_deadline_invalid")
            elapsed_ms = (ended - started).total_seconds() * 1000
        except (AttributeError, TypeError, ValueError) as exc:
            raise AcceptanceError("native_case_deadline_invalid") from exc
        require(
            0 <= elapsed_ms <= specs[cid]["timeout_s"] * 1000 and abs(elapsed_ms - final["duration_ms"]) <= 2,
            "case_deadline_invalid",
        )
        fixture = word_fixture(cid, expected_run_id)
        observed = worker["observed_safe"]
        require(
            set(artifact_bytes) == {"fixture_sha256", "artifact_sha256"}
            and artifact_bytes["fixture_sha256"] == fixture.content
            and hashlib.sha256(artifact_bytes["artifact_sha256"]).hexdigest() == observed["artifact_sha256"]
            and len(artifact_bytes["artifact_sha256"]) == observed["artifact_size_bytes"],
            "retained_artifact_invalid",
        )
        if root_failure is None:
            layers[cid] = "isolated-live"
    return {
        "status": summary["status"],
        "case_layers": layers,
        "case_statuses": {cid: outcomes.get(cid, "NOT_RUN") for cid in specs},
        "results": rows,
        "root_failure": root_failure,
        "evidence_valid": True,
        "receipt_sha256": receipt_sha256,
        "run_id": expected_run_id,
        "go_emitted": False,
    }


def _require_candidate_file_bytes(candidate_sha: str) -> None:
    """Hash every tracked blob independently of Git's cached stat/index flags."""
    from tools import quality_gate as gate

    for option in ("-v", "-f"):
        entries = gate._git_output(ROOT, "ls-files", option, "-z").split("\x00")
        if any(not entry.startswith("H ") for entry in entries if entry):
            raise AcceptanceError("gate_candidate_not_frozen")
    entries = gate._git_output(ROOT, "ls-tree", "-r", "-z", "--full-tree", candidate_sha).split("\x00")
    for entry in entries:
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        path = ROOT / relative
        before = path.lstat()
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or path.resolve(strict=True) != path
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or bool(before.st_mode & 0o111) != (mode == "100755")
            or not 0 <= before.st_size <= 1 << 30
        ):
            raise AcceptanceError("gate_candidate_not_frozen")
        digest = hashlib.sha1(usedforsecurity=False)  # Git object identity, not authentication.
        digest.update(f"blob {before.st_size}\x00".encode())
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            if gate._stat_identity(os.fstat(descriptor)) != gate._stat_identity(before):
                raise AcceptanceError("gate_candidate_not_frozen")
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise AcceptanceError("gate_candidate_not_frozen")
                digest.update(chunk)
                remaining -= len(chunk)
            if (
                os.read(descriptor, 1)
                or digest.hexdigest() != oid
                or gate._stat_identity(os.fstat(descriptor)) != gate._stat_identity(before)
                or gate._stat_identity(path.lstat()) != gate._stat_identity(before)
            ):
                raise AcceptanceError("gate_candidate_not_frozen")
        finally:
            os.close(descriptor)


def _frozen_gate_identity(
    candidate_sha: str, base_sha: str, wheel_sha256: str, inventory: Any
) -> dict[str, str]:
    from tools import quality_gate as gate

    if (
        not isinstance(candidate_sha, str)
        or not isinstance(base_sha, str)
        or not isinstance(wheel_sha256, str)
        or re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None
        or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None
        or base_sha == candidate_sha
        or re.fullmatch(r"[0-9a-f]{64}", wheel_sha256) is None
    ):
        raise AcceptanceError("gate_expected_identity_invalid")
    try:
        if gate._git_output(ROOT, "rev-parse", "HEAD") != candidate_sha or gate._git_output(
            ROOT, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"
        ):
            raise AcceptanceError("gate_candidate_not_frozen")
        tree = gate._git_output(ROOT, "rev-parse", "HEAD^{tree}")
        gate._git_output(ROOT, "merge-base", "--is-ancestor", base_sha, candidate_sha)
        gate._require_candidate_launcher(candidate_sha)
        _require_candidate_file_bytes(candidate_sha)
        for relative in (
            WRAPPER_RELATIVE,
            "tools/release_1_0_capability_matrix.json",
            "tools/quality_gate_inventory.tsv",
            "tools/release_1_0_live_journeys.py",
            "tools/release_1_0_live_cases.py",
            "tools/release_1_0_native.py",
            "tools/release_1_0_deterministic.py",
        ):
            fields = (
                gate._git_output(ROOT, "ls-tree", candidate_sha, "--", relative).partition("\t")[0].split()
            )
            details = (ROOT / relative).lstat()
            if (
                len(fields) != 3
                or fields[0] not in {"100644", "100755"}
                or fields[1] != "blob"
                or gate._git_output(ROOT, "hash-object", "--no-filters", "--", relative) != fields[2]
                or gate._git_output(ROOT, "ls-files", "-v", "--", relative) != f"H {relative}"
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
            ):
                raise AcceptanceError("gate_candidate_not_frozen")
        inventory.validate_candidate_modules(ROOT, candidate_sha)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AcceptanceError("gate_candidate_not_frozen") from exc
    return {
        "candidate_sha": candidate_sha,
        "base_sha": base_sha,
        "candidate_tree": tree,
        "wheel_sha256": wheel_sha256,
        "inventory_sha256": inventory.digest,
    }


def _require_gate_policy_evidence(receipt: Mapping[str, Any], classified: Sequence[Any]) -> None:
    """Check the canonical writer's complete policy projections without a new run."""
    from tools import quality_gate as gate

    def require(condition: bool) -> None:
        if not condition:
            raise AcceptanceError("gate_receipt_policy_invalid")

    def integer(value: Any, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum

    selected = tuple(node for node in classified if node.tier != "nightly")
    groups = (
        ("non-UI", tuple(node for node in selected if node.execution_kind != "browser")),
        ("UI", tuple(node for node in selected if node.execution_kind == "browser")),
    )
    topology = {
        "requested_non_ui_workers": 20,
        "requested_ui_workers": 4,
        "effective_non_ui_workers": min(20, len({node.module_path for node in groups[0][1]})),
        "effective_ui_workers": min(4, len({node.module_path for node in groups[1][1]})),
    }
    require(json.dumps(receipt["topology"], sort_keys=True) == json.dumps(topology, sort_keys=True))
    steps = [
        command.name
        for command in gate._tier_static_commands(
            ROOT,
            python=sys.executable,
            tier="exact-release",
            base_sha=receipt["base_sha"],
            candidate_sha=receipt["candidate_sha"],
            environment={},
        )
    ] + [
        "candidate wheel build",
        "candidate wheel verifier",
        "clean-install candidate wheel",
        "one authoritative candidate collection",
    ]
    steps += [f"exact-release {label} tests" for label, nodes in groups if nodes]
    require(receipt["completed_steps"] == steps)
    metrics = receipt["workload_metrics_before_evidence"]
    metric_fields = {"wall_ns", "user_ns", "sys_ns", "max_rss_bytes", "peak_scratch_bytes", "retry_count"}
    require(isinstance(metrics, dict) and set(metrics) == metric_fields | {"boundary"})
    require(metrics["boundary"] == "after scratch cleanup, before summary composition")
    require(all(integer(metrics[key]) for key in metric_fields))
    require(metrics["wall_ns"] > 0 and metrics["max_rss_bytes"] > 0 and metrics["retry_count"] == 0)
    scratch = receipt["scratch_groups"]
    require(isinstance(scratch, list) and len(scratch) == sum(bool(nodes) for _, nodes in groups))
    for row, (label, nodes) in zip(
        scratch, ((label, nodes) for label, nodes in groups if nodes), strict=True
    ):
        counts = {
            "node_count",
            "declared_budget_bytes",
            "baseline_bytes",
            "peak_total_bytes",
            "incremental_peak_bytes",
        }
        require(isinstance(row, dict) and set(row) == counts | {"group", "enforced", "method"})
        require(all(integer(row[key]) for key in counts))
        require(row["group"] == label and row["node_count"] == len(nodes))
        require(row["declared_budget_bytes"] == sum(node.scratch_mb for node in nodes) * 1024 * 1024)
        require(row["incremental_peak_bytes"] == max(0, row["peak_total_bytes"] - row["baseline_bytes"]))
        require(
            row["enforced"] is False and row["method"] == "sampled regular-file peak minus fixed baseline"
        )
        require(metrics["peak_scratch_bytes"] >= row["peak_total_bytes"])
    host = receipt["release_host_capacity"]
    require(
        isinstance(host, dict)
        and set(host) == {"effective_cpus", "initial_scratch_free_bytes", "host_contour"}
    )
    require(integer(host["effective_cpus"], gate._EXACT_CPU_FLOOR))
    require(integer(host["initial_scratch_free_bytes"], gate._EXACT_SCRATCH_FREE_FLOOR))
    contour = host["host_contour"]
    packages = {
        "dpkg_query": "dpkg",
        "dpkg": "dpkg",
        "apparmor_parser": "apparmor",
        "apparmor_policy": "apparmor",
        "bubblewrap": "bubblewrap",
    }
    require(
        isinstance(contour, dict) and set(contour) == set(packages) | {"os", "userns_restriction", "smoke"}
    )
    require(contour["os"] == {"id": "ubuntu", "version": "26.04", "architecture": "x86_64"})
    require(type(contour["userns_restriction"]) is int and contour["userns_restriction"] == 1)
    smoke = {
        "user_namespace_distinct": True,
        "network_namespace_distinct": True,
        "profile_stack": "bwrap//&unpriv_bwrap (enforce)",
    }
    require(json.dumps(contour["smoke"], sort_keys=True) == json.dumps(smoke, sort_keys=True))
    for key, package in packages.items():
        item = contour[key]
        require(
            isinstance(item, dict)
            and set(item) == {"sha256", "size_bytes", "mode", "package", "version", "architecture"}
        )
        require(
            item["package"] == package and integer(item["size_bytes"], 1) and item["size_bytes"] <= 64 << 20
        )
        require(isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None)
        require(
            all(
                isinstance(item[field], str)
                and 0 < len(item[field]) <= 256
                and item[field].strip() == item[field]
                for field in ("version", "architecture")
            )
        )
        require(isinstance(item["mode"], str) and re.fullmatch(r"[0-7]{4}", item["mode"]) is not None)
        mode = int(item["mode"], 8)
        require(not mode & 0o022 and (key == "apparmor_policy" or bool(mode & 0o111)))


def audit_gate_execution(
    matrix: Mapping[str, Any],
    nodeids: Sequence[str],
    *,
    receipt_path: Path,
    receipt_sha256: str,
    expected_identity: Mapping[str, str],
    inventory: Any,
) -> dict[str, Any]:
    """Validate canonical deterministic and browser evidence, excluding native/device."""
    from tools import quality_gate as gate
    from tools.quality_gate_inventory import InventoryError

    expected_keys = {
        "base_sha": 40,
        "candidate_sha": 40,
        "candidate_tree": 40,
        "wheel_sha256": 64,
        "inventory_sha256": 64,
    }
    if set(expected_identity) != set(expected_keys) or any(
        not isinstance(expected_identity[key], str)
        or re.fullmatch(rf"[0-9a-f]{{{length}}}", expected_identity[key]) is None
        for key, length in expected_keys.items()
    ):
        raise AcceptanceError("gate_expected_identity_invalid")
    receipt = _read_bound_gate_receipt(receipt_path, receipt_sha256)
    if (
        set(receipt) - {"r10_deterministic"}
        != {
            "schema",
            "result",
            "certification_eligible",
            "candidate_sha",
            "candidate_tree",
            "base_sha",
            "tier",
            "inventory_sha256",
            "invariant_identity",
            "wheel_sha256",
            "test_runtime_wheel_sha256",
            "comparison_wheel",
            "topology",
            "release_host_capacity",
            "completed_steps",
            "partition",
            "executed",
            "scratch_groups",
            "workload_metrics_before_evidence",
            "active_deadlines",
            "owned_commands",
            "auxiliary_commands",
        }
        or receipt.get("invariant_identity") != "semantic-function+exact-parameter-set"
    ):
        raise AcceptanceError("gate_receipt_shape_invalid")
    if (
        receipt.get("schema") != "friday.quality-gate-summary.v2"
        or receipt.get("tier") != "exact-release"
        or receipt.get("result") != "passed"
        or receipt.get("certification_eligible") is not True
    ):
        raise AcceptanceError("gate_receipt_not_exact_acceptance")
    topology = receipt.get("topology")
    if (
        not isinstance(topology, dict)
        or topology.get("requested_non_ui_workers") != 20
        or topology.get("requested_ui_workers") != 4
        or expected_identity["base_sha"] == expected_identity["candidate_sha"]
    ):
        raise AcceptanceError("gate_receipt_not_exact_acceptance")
    if any(receipt.get(key) != value for key, value in expected_identity.items()) or (
        inventory.digest != expected_identity["inventory_sha256"]
        or receipt.get("test_runtime_wheel_sha256") != expected_identity["wheel_sha256"]
    ):
        raise AcceptanceError("gate_receipt_identity_mismatch")
    metrics = receipt.get("workload_metrics_before_evidence")
    if (
        receipt.get("comparison_wheel")
        != {"epoch_commit": None, "expected_sha256": None, "observed_sha256": None, "build_profile": None}
        or not isinstance(metrics, dict)
        or type(metrics.get("retry_count")) is not int
        or metrics["retry_count"] != 0
    ):
        raise AcceptanceError("gate_receipt_retry_or_comparison")
    try:
        classified = inventory.classify(tuple(nodeids))
    except InventoryError as exc:
        raise AcceptanceError("gate_receipt_partition_invalid") from exc
    # Compare canonical encodings so boolean values cannot masquerade as ints.
    if json.dumps(receipt.get("partition"), sort_keys=True) != json.dumps(
        gate._partition_evidence(classified), sort_keys=True
    ):
        raise AcceptanceError("gate_receipt_partition_invalid")
    _require_gate_policy_evidence(receipt, classified)
    expected_nodes = {node.nodeid: node for node in classified if node.tier != "nightly"}
    executed = receipt.get("executed")
    seen: set[str] = set()
    duration_by_node: dict[str, int] = {}
    if not isinstance(executed, list) or not executed or len(executed) != len(expected_nodes):
        raise AcceptanceError("gate_receipt_execution_invalid")
    for row in executed:
        if (
            not isinstance(row, dict)
            or set(row) != {"nodeid", "duration_ns"}
            or not isinstance(row["nodeid"], str)
            or row["nodeid"] not in expected_nodes
            or row["nodeid"] in seen
            or type(row["duration_ns"]) is not int
            or not 0 <= row["duration_ns"] <= expected_nodes[row["nodeid"]].max_runtime_s * 1_000_000_000
        ):
            raise AcceptanceError("gate_receipt_execution_invalid")
        seen.add(row["nodeid"])
        duration_by_node[row["nodeid"]] = row["duration_ns"]
    bindings = audit_case_bindings(matrix, nodeids)
    if not bindings["valid"]:
        raise AcceptanceError("gate_receipt_case_bindings_invalid")
    from tools import release_1_0_deterministic as deterministic

    child_receipts = {}
    for case in matrix["cases"]:
        _case_timeout_ns(case)
    if deterministic._journey_bindings(dict(matrix), seen) or "r10_deterministic" in receipt:
        try:
            child_receipts = deterministic.validate_gate_journeys(
                receipt.get("r10_deterministic"),
                dict(expected_identity),
                dict(matrix),
                duration_by_node,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise AcceptanceError("gate_receipt_journey_evidence_invalid") from exc
    case_layers = {}
    case_durations = {}
    for case in matrix["cases"]:
        limit_ns = _case_timeout_ns(case)
        if (
            case["id"] not in bindings["verified_case_ids"]
            or case["layer"] not in {"deterministic", "user-ui"}
            or not set(case["node_ids"]).issubset(seen)
        ):
            continue
        if case["layer"] == "user-ui" and (
            case["execution_driver"] != "canonical-pytest"
            or any(expected_nodes[node].execution_kind != "browser" for node in case["node_ids"])
        ):
            # A completed non-browser node is not evidence of the required UI
            # layer. The validated canonical phase ledger below must also prove
            # every browser member's observed execution and owned cleanup.
            continue
        # JUnit remains an independent completed-duration bound in addition
        # to the observed START/FINISH ledger enforced by the canonical parent.
        duration_ns = sum(duration_by_node[node] for node in case["node_ids"])
        if duration_ns > limit_ns:
            raise AcceptanceError(f"gate_receipt_case_deadline_exceeded:{case['id']}")
        case_durations[case["id"]] = {
            "basis": "sum_of_canonical_node_durations",
            "duration_ns": duration_ns,
            "limit_ns": limit_ns,
        }
        case_layers[case["id"]] = case["layer"]
    from tools import quality_gate_phase as phase

    try:
        active = receipt["active_deadlines"]
        plan = phase.gate_plan(
            tuple(node for node in classified if node.tier != "nightly"), matrix, active["run_id"]
        )
        phase.validate_deadline_evidence(active, plan)
        # pytest serializes JUnit time to milliseconds. Full protocol intervals
        # include setup/call/teardown; allow only that serialization rounding.
        if any(
            duration_by_node[row["nodeid"]] > row["duration_ns"] + 1_000_000 for row in active["attempts"]
        ):
            raise ValueError("gate_junit_deadline_interval_mismatch")
        phase.validate_process_evidence(receipt["owned_commands"], receipt["completed_steps"], active)
        phase.validate_auxiliary_evidence(receipt["auxiliary_commands"], receipt["owned_commands"], active)
        if active["last_observed_ns"] - receipt["owned_commands"][0]["start_ns"] > metrics["wall_ns"]:
            raise ValueError("gate_observed_wall_interval_mismatch")
        if active["retry_count"] != metrics["retry_count"]:
            raise ValueError("gate_retry_count_mismatch")
    except (ValueError, TypeError, KeyError) as exc:
        raise AcceptanceError("gate_receipt_active_deadlines_invalid") from exc
    return {
        "status": "PASS",
        "scope": "canonical exact-gate deterministic and registered browser evidence only",
        "receipt_sha256": receipt_sha256,
        "identity": dict(expected_identity),
        "executed_nodes": len(seen),
        "case_layers": case_layers,
        "case_durations": case_durations,
        "child_receipts": child_receipts,
        "go_emitted": False,
    }


def _count_by(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row[key])] = counts.get(str(row[key]), 0) + 1
    return counts


def audit_sealed_batteries() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "tools"))
    import synthetic_live_acceptance as acceptance  # noqa: E402
    import synthetic_live_battery as battery  # noqa: E402

    pair = battery.audit_frozen_manifests()
    focused = acceptance.inventory_for_suite("focused")
    p06 = acceptance.inventory_for_suite("p06")
    combined = acceptance.inventory_for_suite("all")
    complaints: list[str] = []
    if pair.get("valid") is not True or pair.get("cases") != 400:
        complaints.append("sealed_pair_invalid")
    if combined.get("cases") != 160 or focused.get("cases") != 120 or p06.get("cases") != 40:
        complaints.append("sealed_acceptance_counts_invalid")
    if set(focused["pass_ids"]) & set(p06["pass_ids"]):
        complaints.append("sealed_acceptance_overlap")
    return {
        "valid": not complaints,
        "complaints": complaints,
        "pair": {"valid": pair.get("valid"), "cases": pair.get("cases"), "passes": pair.get("passes")},
        "acceptance": {
            "all": combined.get("cases"),
            "focused": focused.get("cases"),
            "p06": p06.get("cases"),
            "pass_ids": combined.get("pass_ids"),
        },
    }


def evaluate_oracle(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    """Code-owned comparison. Expected is frozen before the run."""

    failure_codes: list[str] = []
    if not observed:
        failure_codes.append("empty_observed")
    if expected.get("status_code") is not None and observed.get("status_code") != expected["status_code"]:
        failure_codes.append("status_code_mismatch")
    if expected.get("file_sha256"):
        got = str(observed.get("file_sha256") or "")
        if got != expected["file_sha256"]:
            failure_codes.append("digest_mismatch")
        if not got:
            failure_codes.append("missing_required_artifact")
    if expected.get("must_contain"):
        body = str(observed.get("body") or "")
        for token in expected["must_contain"]:
            if str(token) not in body:
                failure_codes.append("missing_required_token")
                break
    foreign = expected.get("foreign_canaries") or observed.get("foreign_canaries") or []
    body = str(observed.get("body") or "")
    for marker in foreign:
        if marker and str(marker) in body:
            failure_codes.append("privacy_canary_exposed")
            break
    if expected.get("effect_forbidden") is True and observed.get("effect") is True:
        failure_codes.append("forbidden_effect")
    if expected.get("min_count") is not None and int(observed.get("count") or 0) < int(expected["min_count"]):
        failure_codes.append("count_below_minimum")
    if not observed.get("collected", True):
        raise AcceptanceError("zero_collected_cases")
    status = "FAIL" if failure_codes else "PASS"
    return {
        "status": status,
        "failure_codes": failure_codes,
        "expected_outcome": expected.get("expected_outcome"),
        "observed_keys": sorted(observed),
    }


def negative_controls() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    digest = evaluate_oracle(
        {"file_sha256": "a" * 64, "expected_outcome": "pass"},
        {"file_sha256": "b" * 64, "collected": True},
    )
    if digest["status"] != "FAIL" or "digest_mismatch" not in digest["failure_codes"]:
        raise AcceptanceError("negative_control_digest_did_not_fail")
    results.append({"id": "R10-NEG-CTRL-WRONG-DIGEST", "status": "PASS", "proved": "oracle_goes_red"})

    try:
        evaluate_oracle({"expected_outcome": "pass"}, {"collected": False})
    except AcceptanceError as exc:
        if str(exc) != "zero_collected_cases":
            raise
    else:
        raise AcceptanceError("negative_control_empty_collection_did_not_fail")
    results.append({"id": "R10-NEG-CTRL-EMPTY-COLLECTION", "status": "PASS", "proved": "harness_error"})

    canary = evaluate_oracle(
        {"expected_outcome": "pass", "foreign_canaries": ["SYN-FOREIGN-DEADBEEF"]},
        {"body": "ok SYN-FOREIGN-DEADBEEF", "collected": True},
    )
    if canary["status"] != "FAIL" or "privacy_canary_exposed" not in canary["failure_codes"]:
        raise AcceptanceError("negative_control_canary_did_not_fail")
    results.append({"id": "R10-NEG-CTRL-FOREIGN-CANARY", "status": "PASS", "proved": "oracle_goes_red"})
    return {"valid": True, "results": results}


def preflight(*, root: Path = ROOT) -> dict[str, Any]:
    complaints: list[str] = []
    python = sys.version_info
    if (python.major, python.minor) < (3, 13):
        complaints.append("python_too_old")
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file() or not os.access(bwrap, os.X_OK):
        complaints.append("bwrap_missing")
    scratch = Path("/var/tmp")
    try:
        usage = shutil.disk_usage(scratch)
        free_gi = usage.free / (1024**3)
    except OSError:
        free_gi = 0.0
        complaints.append("var_tmp_unreadable")
    cpus = os.cpu_count() or 0
    for relative in (*CANONICAL_TOOLS, WRAPPER_RELATIVE, "tools/release_1_0_capability_matrix.json"):
        path = root / relative
        if not path.is_file():
            complaints.append(f"missing:{relative}")
    return {
        "valid": not complaints,
        "complaints": complaints,
        "python": f"{python.major}.{python.minor}.{python.micro}",
        "cpus": cpus,
        "var_tmp_free_gi": round(free_gi, 2),
        "bwrap": str(bwrap) if bwrap.is_file() else None,
        "secrets_emitted": False,
        "note": "exact-release still requires 24 CPU and 32 GiB free /var/tmp; this preflight does not lower that floor",
    }


def exclusive_slot_busy() -> list[str]:
    busy: list[str] = []
    try:
        result = subprocess.run(
            ("ps", "-eo", "args="),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["ps_unavailable"]
    text = result.stdout or ""
    needles = (
        "tools/quality_gate.py --tier",
        "tools/synthetic_live_acceptance.py --suite",
        "tools/synthetic_live_battery.py --both",
        "tools/document_contour_live_battery.py",
    )
    for needle in needles:
        if needle in text:
            busy.append(needle)
    return busy


def plan_commands(mode: str) -> dict[str, Any]:
    if mode not in {"diagnostic-baseline", "final"}:
        raise AcceptanceError("unknown_mode")
    common = [
        "umask 077",
        'r10_collection_dir="$(mktemp -d -p /var/tmp friday-r10-collection.XXXXXXXX)"',
        'r10_collection="$r10_collection_dir/nodes.json"',
        '.venv/bin/python -I -B tools/quality_gate.py --inventory-collection "$r10_collection"',
        '.venv/bin/python -I -B tools/quality_gate_inventory.py --collection "$r10_collection" --check',
        '.venv/bin/python -I -B tools/release_1_0_acceptance.py --audit-only --collection "$r10_collection"',
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --preflight",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --negative-control",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py --audit-only",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py --suite all --audit-only",
        (
            ".venv/bin/python -I -B -m pytest -q "
            "tests/test_release_1_0_acceptance.py tests/test_release_1_0_journeys.py "
            "tests/test_release_1_0_coverage.py tests/test_release_1_0_fault_controls.py "
            "tests/test_release_1_0_deterministic.py "
            "tests/test_release_1_0_soak_client.py tests/test_release_1_0_app_soak.py "
            "tests/test_release_1_0_app_soak_observed_controls.py tests/test_release_1_0_soak_receipts.py "
            "tests/test_release_1_0_telegram_roundtrip.py tests/test_release_1_0_telegram_receipts.py "
            "tests/test_release_1_0_live_cases.py tests/test_release_1_0_native.py tests/test_release_1_0_native_assets.py "
            "tests/test_release_1_0_native_process.py tests/test_release_1_0_remaining_oracles.py tests/test_release_1_0_graph_oracles.py tests/test_release_1_0_conversation_oracles.py tests/test_release_1_0_self_conversation_oracles.py tests/test_release_1_0_token_oracles.py tests/test_release_1_0_reminder_oracles.py tests/test_release_1_0_user_oracles.py tests/test_release_1_0_account_oracles.py tests/test_release_1_0_identity_oracles.py tests/test_release_1_0_profile_oracles.py tests/test_release_1_0_chronicle_oracles.py tests/test_release_1_0_knowledge_read_oracles.py tests/test_release_1_0_knowledge_mutation_oracles.py tests/test_release_1_0_entity_queue_oracles.py tests/test_release_1_0_ui_oracles.py tests/test_release_1_0_ui_sources_chats_oracles.py tests/test_release_1_0_ui_three_oracles.py tests/test_release_1_0_data_source_oracles.py tests/test_release_1_0_audit_oracles.py tests/test_release_1_0_lifecycle_conflict_oracles.py tests/test_release_1_0_ops_settings_oracles.py tests/test_release_1_0_ops_diagnostics_oracles.py tests/test_release_1_0_ops_quality_oracles.py tests/test_release_1_0_api_metadata_oracles.py tests/test_release_1_0_inbox_read_oracles.py tests/test_release_1_0_public_health_oracles.py tests/test_release_1_0_preset_oracles.py tests/test_quality_gate_deadlines.py tests/test_quality_gate_phase.py tests/test_quality_gate_bootstrap.py tests/test_quality_gate_process.py "
            "tests/test_release_1_0_secondary_relay.py tests/test_release_1_0_surface_discovery.py "
            "tests/test_release_1_0_mixed_delivery_http.py "
            "tests/test_release_1_0_backup_verify_http.py "
            "tests/test_release_1_0_cli_archive_maintenance.py "
            "tests/test_release_1_0_cli_backup_export_paths.py "
            "tests/test_release_1_0_cli_bridge_body.py "
            "tests/test_release_1_0_cli_command_store_paths.py "
            "tests/test_release_1_0_cli_crypto_paths.py "
            "tests/test_release_1_0_cli_data_source_paths.py "
            "tests/test_release_1_0_cli_eval_bootstrap_paths.py "
            "tests/test_release_1_0_cli_graph_maintenance.py "
            "tests/test_release_1_0_cli_import_purge_paths.py "
            "tests/test_release_1_0_cli_maintenance_dispatch.py "
            "tests/test_release_1_0_cli_model_graph_paths.py "
            "tests/test_release_1_0_cli_read_paths.py "
            "tests/test_release_1_0_cli_restore_paths.py "
            "tests/test_release_1_0_cli_retag_paths.py "
            "tests/test_release_1_0_cli_series_conflicts.py "
            "tests/test_release_1_0_cli_server_process.py "
            "tests/test_release_1_0_cli_setup_paths.py "
            "tests/test_release_1_0_cli_token_paths.py "
            "tests/test_release_1_0_cli_tui_body.py "
            "tests/test_release_1_0_document_map_witness_http.py "
            "tests/test_release_1_0_eval_comparisons_http.py "
            "tests/test_release_1_0_inbox_enrichment_http.py "
            "tests/test_release_1_0_mission_research_actions_http.py "
            "tests/test_release_1_0_obsidian_http_boundaries.py "
            "tests/test_release_1_0_regenerate_http.py "
            "tests/test_release_1_0_semantic_witness_http.py "
            "tests/test_release_1_0_server_startup_resources.py "
            "tests/test_release_1_0_source_router_discovery.py "
            "tests/test_release_1_0_supervisor_startup.py "
            "tests/test_release_1_0_telegram_command_gaps.py "
            "tests/test_synthetic_live_b09_evidence.py "
            "tests/test_graph_runtime_log_privacy.py::test_entity_audit_retains_no_content_or_content_hash_after_hard_purge "
            "tests/test_entity_edits_are_reversible.py::test_http_restore_is_self_service_and_audited "
            "tests/test_entity_edits_are_reversible.py::test_a_deleted_object_can_be_brought_back "
            "tests/test_entity_edits_are_reversible.py::test_restoring_an_entity_version_is_a_new_version_not_a_rewind "
            "tests/test_entity_edits_are_reversible.py::test_restore_does_not_cross_tenants "
            "tests/test_the_audit_log_never_takes_a_document_body.py::test_the_own_edit_and_delete_routes_cannot_copy_a_note_into_audit "
            "tests/test_shared_archive.py::test_one_person_finds_and_edits_what_another_wrote "
            "tests/test_users_can_forget_their_own_knowledge.py::test_a_plain_user_can_delete_their_own_knowledge_object "
            "tests/test_users_can_forget_their_own_knowledge.py::test_a_plain_user_still_cannot_delete_someone_elses_knowledge_object "
            "tests/test_eval_harness.py::test_eval_endpoints_end_to_end "
            "tests/test_eval_harness.py::test_metric_functions "
            "tests/test_eval_harness.py::test_add_list_delete_eval_case "
            "tests/test_eval_harness.py::test_run_eval_measures_and_detects_regression "
            "tests/test_eval_harness.py::test_run_eval_empty_gold_set "
            "tests/test_graph_policy_is_declared.py::test_admin_eval_search_declares_the_policy_and_writes_no_usage "
            "tests/test_retrieval_explain.py::test_retrieval_explain_endpoint "
            "tests/test_retrieval_explain.py::test_score_reconstructs_from_components "
            "tests/test_search_explain_contract.py::test_search_explain_api_is_privacy_safe_and_reports_unavailable_corpora "
            "tests/test_search_explain_contract.py::test_search_explain_api_rejects_unknown_corpus_and_date_role "
            "tests/test_the_compact_tab_has_something_to_show.py::test_the_list_is_empty_before_any_run "
            "tests/test_the_compact_tab_has_something_to_show.py::test_a_run_appears_in_the_list_and_reads_back "
            "tests/test_the_compact_tab_has_something_to_show.py::test_running_the_same_day_twice_makes_one_row "
            "tests/test_the_compact_tab_has_something_to_show.py::test_a_malformed_date_is_refused_not_guessed "
            "tests/test_the_compact_tab_has_something_to_show.py::test_someone_elses_compact_is_only_for_the_owner "
            "tests/test_the_compact_tab_has_something_to_show.py::test_the_list_carries_the_human_wording "
            "tests/test_executive.py::test_mission_http_endpoints_create_list_and_stop "
            "tests/test_mission_oversight_boundaries.py::test_reading_another_accounts_mission_is_recorded "
            "tests/test_mission_oversight_boundaries.py::test_a_delegated_admin_cannot_cancel_the_owners_mission "
            "tests/test_mission_oversight_boundaries.py::test_cancelling_an_ordinary_accounts_mission_still_works_and_is_recorded "
            "tests/test_the_export_says_what_it_is.py::test_body_free_response_does_not_advertise_a_nonexistent_plaintext_vault "
            "tests/test_file_delivery_privacy.py::test_admin_download_revalidates_immediately_before_atomic_read "
            "tests/test_the_export_says_what_it_is.py::test_explicit_full_owner_response_names_the_readable_vault "
            "tests/test_the_export_says_what_it_is.py::test_it_admits_what_it_leaves_behind "
            "tests/test_owner_mutation_boundaries.py::test_delegated_admin_cannot_export_owner_archive "
            "tests/test_export_private_reminder_isolation.py::test_export_keeps_dependencies_of_the_users_exact_private_alias "
            "tests/test_export_private_reminder_isolation.py::test_export_uses_current_and_historical_private_alias_identity_tokens "
            "tests/test_export_private_reminder_isolation.py::test_tenant_export_does_not_include_another_persons_monitor_query_or_chat "
            "tests/test_engineer_terminal_notification_api.py::test_pending_projects_no_raw_handle_and_artifact_is_exact "
            "tests/test_engineer_terminal_notification_api.py::test_revocation_after_stage_retires_without_exposing_bytes[identity] "
            "tests/test_engineer_terminal_notification_api.py::test_revocation_after_stage_retires_without_exposing_bytes[capability] "
            "tests/test_engineer_terminal_notification_api.py::test_revocation_after_stage_retires_without_exposing_bytes[account] "
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_pending_does_not_require_file_read "
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_claim_reauthorizes_after_pointer_listing[identity] "
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_claim_reauthorizes_after_pointer_listing[capability] "
            "tests/test_engineer_terminal_delivery.py::test_terminal_text_claim_reauthorizes_after_pointer_listing[account] "
            "tests/test_organs_reminders.py::test_notification_endpoints_require_bridge_actor "
            "tests/test_notification_queue_head.py::test_a_de_allowlisted_chat_does_not_block_the_queue "
            "tests/test_approval_reaches_the_person.py::test_the_bridge_receives_what_it_needs_to_draw_the_buttons "
            "tests/test_product_quality.py::test_admin_quality_workflows_and_api_ingest_default "
            "tests/test_relation_triage_in_chat.py::test_api_list_and_review_are_tenant_scoped_capability_gated_and_content_free "
            "tests/test_relation_triage_in_chat.py::test_review_is_idempotent_but_terminal_and_audit_is_content_free "
            "tests/test_relation_triage_in_chat.py::test_review_identity_comes_from_actor_not_request_body "
            "tests/test_relation_triage_in_chat.py::test_write_only_grant_cannot_read_or_review_relation_cards "
            "tests/test_conflict_queue_triage_hints.py::test_http_conflict_list_includes_triage_hint "
            "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[assessed] "
            "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[unless_explicit] "
            "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[always] "
            "tests/test_organs_importer.py::test_import_endpoint_queues_reviews_and_is_idempotent "
            "tests/test_organs_importer.py::test_import_endpoint_rejects_unknown_format_and_requires_auth "
            "tests/test_graph_runtime_log_privacy.py::test_resolution_and_merge_surfaces_never_publish_snapshots_or_evidence "
            "tests/test_resolution_queue_is_a_page.py::test_the_next_page_shows_what_the_first_one_hid "
            "tests/test_resolution_queue_is_a_page.py::test_the_queue_reports_the_whole_size_not_the_page_size "
            "tests/test_relations_actually_get_found.py::test_accepting_a_link_reconsiders_the_relations "
            "tests/test_relations_actually_get_found.py::test_rejecting_a_link_proposes_nothing "
            "tests/test_conflict_triage_in_chat.py::test_http_conflict_decide_dismisses_and_hides_from_suggested "
            "tests/test_admin_entity_mutation_http.py::test_admin_entity_create_persists_exact_target_row_and_first_version "
            "tests/test_admin_entity_mutation_http.py::test_admin_entity_patch_preserves_foreign_rows_and_appends_exact_revision "
            "tests/test_admin_entity_mutation_http.py::test_admin_entity_delete_records_tombstone_once_and_preserves_other_tenant "
            "tests/test_maturity_070.py::test_feedback_state_replaces_rating_and_updates_usage_attribution "
            "tests/test_maturity_070.py::test_current_feedback_stats_replace_superseded_signal "
            "tests/test_document_contour_observer_snapshot.py::test_http_snapshot_is_owner_only_and_numeric_loopback_only "
            "tests/test_production_read_only_observation_route.py::test_real_lifespan_collector_uses_the_existing_storage_connection "
            "tests/test_approval_reaches_the_person.py::test_the_route_executes_on_approval_and_only_once "
            "tests/test_approval_reaches_the_person.py::test_a_rejection_does_not_execute "
            "tests/test_approval_reaches_the_person.py::test_an_approval_that_cannot_execute_says_so "
            "tests/test_approval_reaches_the_person.py::test_the_chat_command_lists_what_waits_and_names_unknown_outcomes "
            "tests/test_approval_reaches_the_person.py::test_a_bystander_pressing_the_button_changes_nothing "
            "tests/test_one_operation_through_every_surface.py::test_a_stranger_sees_nothing_on_any_surface "
            "tests/test_containers_browse.py::test_http_tags_containers_and_filters "
            "tests/test_event_timeline.py::test_timeline_and_set_time_over_http "
            "tests/test_event_timeline.py::test_unified_timeline_page_is_exposed_over_http "
            "tests/test_containers_browse.py::test_create_container_validates_kind_parent_and_builds_part_of "
            "tests/test_containers_browse.py::test_container_knowledge_count_reflects_accepted_members "
            "tests/test_containers_browse.py::test_list_knowledge_tags_counts_casefold_and_excludes_deleted "
            "tests/test_event_timeline.py::test_set_event_time_validates_type_dates_and_range "
            "tests/test_event_timeline.py::test_timeline_page_unifies_events_and_relation_changes_under_one_limit "
            "tests/test_source_search.py::test_source_search_over_http_excludes_rejected_material "
            "tests/test_timeline_in_chat.py::test_the_period_total_comes_from_the_route_not_from_the_page "
            "tests/test_shared_archive.py::test_without_the_setting_isolation_is_intact "
            "tests/test_monitors_watch_a_topic.py::test_monitors_are_self_service_over_http "
            "tests/test_organs_reflection.py::test_reflection_endpoint_returns_digest_for_actor "
            "tests/test_monitors_watch_a_topic.py::test_a_foreign_monitor_cannot_be_stopped "
            "tests/test_monitors_watch_a_topic.py::test_a_person_cannot_hoard_monitors "
            "tests/test_organs_reflection.py::test_build_reflection_summarises_state "
            "tests/test_bridge_events.py::test_a_bridge_event_lands_in_the_journal "
            "tests/test_bridge_events.py::test_an_unknown_event_type_is_refused "
            "tests/test_bridge_events.py::test_the_endpoint_requires_bridge_authentication "
            "tests/test_bridge_events.py::test_the_payload_is_bounded_on_both_axes "
            "'tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.dead_letter]' "
            "'tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.outbound_failed]' "
            "'tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.outbound_recovered]' "
            "'tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.poll_failed]' "
            "'tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.poll_recovered]'"
        ),
    ]
    exact = [
        "candidate_sha=\"$(git rev-parse --verify 'HEAD^{commit}')\"",
        'base_sha="$(git rev-parse --verify "${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}")"',
        'evidence_dir="$(mktemp -d -p /var/tmp friday-exact-evidence.XXXXXXXX)"',
        ".venv/bin/python -I -B tools/quality_gate.py --tier exact-release "
        '--candidate-sha "$candidate_sha" --base-sha "$base_sha" --evidence-dir "$evidence_dir"',
    ]
    additional_live = [
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py "
        '--env-file "$FRIDAY_ENV_FILE" --suite all --concurrency 4',
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_live_journeys.py "
        '--env-file "$FRIDAY_ENV_FILE" --candidate-sha "$candidate_sha" '
        '--evidence-dir "$evidence_dir/r10-native" --run-live',
    ]
    b09_preflight = [
        'test -n "${FRIDAY_ENV_FILE:-}"',
        'test -f "$FRIDAY_ENV_FILE" && test ! -L "$FRIDAY_ENV_FILE"',
        'test "$(stat -c %a -- "$FRIDAY_ENV_FILE")" = 600',
        "umask 077",
        'test -n "${R10_B09_REVIEW_PLAN:-}"',
        'test -n "${R10_B09_ROOT_KEY:-}"',
        'test -n "${R10_B09_PAIR_ROOT:-}"',
        'test -f "$R10_B09_REVIEW_PLAN" && test ! -L "$R10_B09_REVIEW_PLAN"',
        'test -f "$R10_B09_ROOT_KEY" && test ! -L "$R10_B09_ROOT_KEY"',
        'test "$(stat -c %a -- "$R10_B09_REVIEW_PLAN")" = 600',
        'test "$(stat -c %a -- "$R10_B09_ROOT_KEY")" = 600',
        'case "$R10_B09_PAIR_ROOT" in /*) ;; *) false ;; esac',
        'test ! -e "$R10_B09_PAIR_ROOT" && test ! -L "$R10_B09_PAIR_ROOT"',
    ]
    b09_closed_run = (
        "if PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py "
        '--env-file "$FRIDAY_ENV_FILE" --both --concurrency 4 '
        '--run-directory "$R10_B09_PAIR_ROOT" '
        '--b09-review-plan "$R10_B09_REVIEW_PLAN" '
        '--root-review-key "$R10_B09_ROOT_KEY"; then false; '
        'else r10_b09_closed_rc=$?; test "$r10_b09_closed_rc" = 4; fi'
    )
    b09_task = (
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_b09_evidence.py task "
        '--key-file "$R10_B09_ROOT_KEY" --plan "$R10_B09_REVIEW_PLAN" '
        '--pair-report "$R10_B09_PAIR_ROOT/pair-aggregate.json" '
        '--b03-evidence "$R10_B09_PAIR_ROOT/battery-b/pass-03/evidence/raw-responses.jsonl" '
        '--b09-evidence "$R10_B09_PAIR_ROOT/battery-b/pass-09/evidence/raw-responses.jsonl" '
        '--output "${R10_B09_PAIR_ROOT%/*}/review-task.json"'
    )
    b09_post_review = [
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_b09_evidence.py issue "
        '--key-file "$R10_B09_ROOT_KEY" --plan "$R10_B09_REVIEW_PLAN" '
        '--task "${R10_B09_PAIR_ROOT%/*}/review-task.json" '
        '--result "${R10_B09_PAIR_ROOT%/*}/review-result.json" '
        '--output-directory "${R10_B09_PAIR_ROOT%/*}/receipts"',
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_b09_evidence.py bind "
        '--key-file "$R10_B09_ROOT_KEY" --plan "$R10_B09_REVIEW_PLAN" '
        '--pair-report "$R10_B09_PAIR_ROOT/pair-aggregate.json" '
        '--receipt-directory "${R10_B09_PAIR_ROOT%/*}/receipts" '
        '--acceptance-output "${R10_B09_PAIR_ROOT%/*}/content-acceptance.json" '
        '--output "${R10_B09_PAIR_ROOT%/*}/final-pair.json"',
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_b09_evidence.py verify "
        '--key-file "$R10_B09_ROOT_KEY" '
        '--final-pair "${R10_B09_PAIR_ROOT%/*}/final-pair.json"',
    ]
    # Both plans retain the complete official 400-case A+B scope.  The battery
    # validates the signed plan and exact pair_directory before constructing an
    # executor; immediate TASK construction prevents a red pair reaching the lab.
    live = [*b09_preflight, b09_closed_run, b09_task, *additional_live]
    telegram_deployment_device = [
        'test -n "${R10_TELEGRAM_POLICY:-}"',
        'test -n "${R10_TELEGRAM_EVIDENCE_DIR:-}"',
        'test -n "${R10_TELEGRAM_CONTEXT:-}"',
        'test -n "${R10_TELEGRAM_CONTEXT_SHA256:-}"',
        'test -n "${R10_TELEGRAM_RECEIPT:-}"',
        'test -n "${R10_TELEGRAM_RECEIPT_SHA256:-}"',
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B "
        'tools/release_1_0_telegram_roundtrip.py --policy "$R10_TELEGRAM_POLICY" '
        '--evidence-dir "$R10_TELEGRAM_EVIDENCE_DIR"',
        ".venv/bin/python -I -B tools/release_1_0_acceptance.py --audit-only "
        '--collection "$r10_collection" --telegram-receipt "$R10_TELEGRAM_RECEIPT" '
        '--telegram-receipt-sha256 "$R10_TELEGRAM_RECEIPT_SHA256" '
        '--telegram-context "$R10_TELEGRAM_CONTEXT" '
        '--telegram-context-sha256 "$R10_TELEGRAM_CONTEXT_SHA256"',
    ]
    return {
        "mode": mode,
        "purpose": (
            "non-certifying diagnostic baseline"
            if mode == "diagnostic-baseline"
            else "final release evidence input; root still owns GO"
        ),
        "order": [
            "harness and matrix audit",
            "exact-release quality_gate (canonical, no imported receipt)",
            "preflight the signed review plan, root key path and fixed absent pair root",
            "complete official 400-case A then B; B only if A is green; closed green exits 4",
            "sealed review TASK from the closed B03 and B09 evidence; red fails before lab delivery",
            "canonical 160-case live acceptance and additional R10 journeys on the frozen candidate",
            "actual preregistered lab review remains mandatory and externally pending",
            "root issues plan-required receipts, binds the final pair, and requires verifier exit 0",
            "dedicated test-bot deployment-device attempt only when its independent context exists",
        ],
        "commands": {
            "harness": common,
            "exact_release": exact,
            "live": live,
            "b09_post_review": b09_post_review,
            "telegram_deployment_device": telegram_deployment_device,
        },
        "stages": [
            {"id": "harness", "kind": "commands", "command_group": "harness", "required": True},
            {
                "id": "exact_release",
                "kind": "commands",
                "command_group": "exact_release",
                "required": True,
            },
            {"id": "live", "kind": "commands", "command_group": "live", "required": True},
            {
                "id": "actual_content_review",
                "kind": "external",
                "state": "pending_until_actual_preregistered_result",
                "required": True,
            },
            {
                "id": "root_bind_and_verify",
                "kind": "commands",
                "command_group": "b09_post_review",
                "required": True,
            },
            {
                "id": "telegram_deployment_device",
                "kind": "commands",
                "command_group": "telegram_deployment_device",
                "required": "when_independent_context_exists",
            },
        ],
        "coverage_denominators": {"canonical_live": 160, "official_ab": 400},
        "b09_final": {
            "required": True,
            "mode_purpose": (
                "diagnostic_only_no_release_go"
                if mode == "diagnostic-baseline"
                else "final_evidence_only_root_owns_go"
            ),
            "required_inputs": [
                "R10_B09_REVIEW_PLAN",
                "R10_B09_ROOT_KEY",
                "R10_B09_PAIR_ROOT",
            ],
            "review_evidence_inputs": [
                "$R10_B09_PAIR_ROOT/battery-b/pass-03/evidence/raw-responses.jsonl",
                "$R10_B09_PAIR_ROOT/battery-b/pass-09/evidence/raw-responses.jsonl",
            ],
            "review_semantics": "plan-bound B03/B09 source facts and rubric",
            "closed_run_expected_exit": 4,
            "closed_run_is_acceptance": False,
            "pre_execution_binding": (
                "signed plan and exact resolved absent pair path are validated before executor construction"
            ),
            "outstanding_after_closed_run": [
                "sealed_review_task",
                "actual_preregistered_lab_review",
                "root_issue_bind_verify",
            ],
            "actual_lab_review": "mandatory_preregistered_external_stage",
            "root_binder_verify": "mandatory_exit_0",
            "acceptance_authority": "root-key verification of the signed final pair, exit 0",
        },
        "cannot": [
            "emit GO",
            "start B after a red A",
            "treat a generated command, exit 4, summaries, or counters as release acceptance",
            "skip required cases because of budget",
            "rewrite sealed A/B manifests",
            "use the live production home or live Telegram singleton",
            "overlap another full native/UI/model-heavy gate",
        ],
        "go_rule": "conjunction of required cases, no open high/critical defects, no required gaps, exact identities; this wrapper never prints GO",
    }


def matrix_summary(
    matrix: Mapping[str, Any], classified: Mapping[str, Any], sealed: Mapping[str, Any]
) -> dict[str, Any]:
    cases = [case for case in matrix["cases"] if isinstance(case, dict)]
    executable = [case for case in cases if case.get("executable") is True]
    required = [case for case in cases if case.get("release_required") is True]
    live = [case for case in required if case.get("layer") == "isolated-live"]
    blocked = [case for case in cases if case.get("executable") is False]
    return {
        "schema": SCHEMA,
        "revision": matrix.get("revision"),
        "matrix_sha256": _sha256_file(MATRIX_PATH),
        "wrapper_sha256": _sha256_file(Path(__file__).resolve()),
        "capabilities": len(matrix["capabilities"]),
        "additional_cases": len(cases),
        "additional_executable": len(executable),
        "additional_required": len(required),
        "additional_required_executable": sum(case.get("executable") is True for case in required),
        "additional_required_live": len(live),
        "additional_not_executable": [
            {"id": case["id"], "reason": case.get("blocked_reason")} for case in blocked
        ],
        "sealed_unique_cases": 400,
        "sealed_acceptance_executions": 160,
        "surface_unknown": classified.get("unknown"),
        "surface_coverage_gaps": classified.get("coverage_gaps"),
        "sealed_audit_valid": sealed.get("valid"),
        "go_emitted": False,
        "product_accepted_1_0": False,
    }


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _read_native_context(path: Path, digest: str) -> dict[str, Any]:
    """Read caller-frozen expectations independently of native output."""
    try:
        context = _read_bound_gate_receipt(path, digest)
    except AcceptanceError as exc:
        raise AcceptanceError("native_context_invalid") from exc
    lengths = {
        "candidate_sha": 40,
        "candidate_tree": 40,
        "candidate_source_sha256": 64,
        "wheel_sha256": 64,
        "installed_site_sha256": 64,
        "suite_sha256": 64,
        "model_environment_sha256": 64,
    }
    identity = context.get("identity")
    if (
        set(context)
        != {"schema", "base_sha", "identity", "run_id", "case_ids", "secondary_enabled", "secondary_mode"}
        or context.get("schema") != "friday.r10-native-context.v1"
        or not isinstance(identity, dict)
        or set(identity) != set(lengths)
        or any(
            not isinstance(identity[key], str)
            or re.fullmatch(rf"[0-9a-f]{{{length}}}", identity[key]) is None
            for key, length in lengths.items()
        )
        or not isinstance(context.get("base_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", context["base_sha"]) is None
        or context["base_sha"] == identity["candidate_sha"]
        or not isinstance(context.get("run_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", context["run_id"]) is None
        or not isinstance(context.get("case_ids"), list)
        or not context["case_ids"]
        or not all(isinstance(cid, str) for cid in context["case_ids"])
        or len(set(context["case_ids"])) != len(context["case_ids"])
        or type(context.get("secondary_enabled")) is not bool
        or not (context.get("secondary_mode") is None or isinstance(context.get("secondary_mode"), str))
        or (
            context["secondary_mode"] not in {"shadow", "assist"}
            if context["secondary_enabled"]
            else context["secondary_mode"] not in {None, "disabled"}
        )
    ):
        raise AcceptanceError("native_context_invalid")
    return context


def _frozen_native_identity(context: Mapping[str, Any], inventory: Any) -> dict[str, str]:
    """Bind external expectations to current candidate and native source bytes."""
    from tools import release_1_0_native as native
    from tools import synthetic_live_battery as battery

    expected = context["identity"]
    local = _frozen_gate_identity(
        expected["candidate_sha"], context["base_sha"], expected["wheel_sha256"], inventory
    )
    if local["candidate_tree"] != expected["candidate_tree"]:
        raise AcceptanceError("native_context_candidate_mismatch")
    try:
        paths = battery._candidate_source_paths(
            root=ROOT,
            instrument_path=ROOT / "tools/release_1_0_native.py",
            manifest_paths=[ROOT / "tools/release_1_0_capability_matrix.json"],
        )
        source_digest = battery._candidate_source_digest(root=ROOT, relative_paths=paths)
        suite_digest = native._suite_digest(ROOT)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise AcceptanceError("native_candidate_not_frozen") from exc
    if source_digest != expected["candidate_source_sha256"] or suite_digest != expected["suite_sha256"]:
        raise AcceptanceError("native_context_candidate_mismatch")
    return dict(expected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Friday 1.0 acceptance wrapper (not a second gate)")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--negative-control", action="store_true")
    parser.add_argument("--plan", choices=("diagnostic-baseline", "final"))
    parser.add_argument(
        "--collection", type=Path, help="Exact canonical quality-gate collection for audit bindings"
    )
    parser.add_argument("--gate-receipt", type=Path, help="Private canonical exact-release summary")
    parser.add_argument("--gate-receipt-sha256", help="Independently retained exact-release receipt digest")
    parser.add_argument("--candidate-sha", help="Frozen candidate commit for the supplied gate receipt")
    parser.add_argument("--base-sha", help="Frozen ancestor commit for the supplied gate receipt")
    parser.add_argument("--wheel-sha256", help="Frozen candidate wheel digest for the supplied gate receipt")
    parser.add_argument("--native-receipt", type=Path, help="Private native summary.json")
    parser.add_argument("--native-receipt-sha256", help="Independently retained native summary digest")
    parser.add_argument("--native-context", type=Path, help="Separate caller-frozen native expectations")
    parser.add_argument("--native-context-sha256", help="Independently retained native context digest")
    parser.add_argument("--app-soak-receipt", type=Path, help="Private owned frozen-app-soak result")
    parser.add_argument("--app-soak-receipt-sha256")
    parser.add_argument(
        "--app-soak-context", type=Path, help="Independent pre-run expected policy/configuration"
    )
    parser.add_argument("--app-soak-context-sha256")
    parser.add_argument("--telegram-receipt", type=Path, help="Private owned live-Telegram attempt receipt")
    parser.add_argument("--telegram-receipt-sha256")
    parser.add_argument("--telegram-context", type=Path, help="Independent pre-run Telegram expectations")
    parser.add_argument("--telegram-context-sha256")
    args = parser.parse_args(argv)
    selected = [bool(args.audit_only), bool(args.preflight), bool(args.negative_control), bool(args.plan)]
    if sum(selected) != 1:
        parser.error("choose exactly one of --audit-only, --preflight, --negative-control, --plan")
    receipt_args = (
        args.gate_receipt,
        args.gate_receipt_sha256,
        args.candidate_sha,
        args.base_sha,
        args.wheel_sha256,
    )
    if any(value is not None for value in receipt_args) and (
        not args.audit_only or args.collection is None or not all(value is not None for value in receipt_args)
    ):
        parser.error(
            "gate evidence requires --audit-only, --collection and all five receipt identity arguments"
        )
    native_args = (
        args.native_receipt,
        args.native_receipt_sha256,
        args.native_context,
        args.native_context_sha256,
    )
    if any(value is not None for value in native_args) and (
        not args.audit_only or args.collection is None or not all(value is not None for value in native_args)
    ):
        parser.error("native evidence requires --audit-only, --collection and all four native arguments")
    app_soak_args = (
        args.app_soak_receipt,
        args.app_soak_receipt_sha256,
        args.app_soak_context,
        args.app_soak_context_sha256,
    )
    if any(value is not None for value in app_soak_args) and (
        not args.audit_only
        or args.collection is None
        or not all(value is not None for value in app_soak_args)
    ):
        parser.error("app-soak evidence requires --audit-only, --collection and all four app-soak arguments")
    telegram_args = (
        args.telegram_receipt,
        args.telegram_receipt_sha256,
        args.telegram_context,
        args.telegram_context_sha256,
    )
    if any(value is not None for value in telegram_args) and (
        not args.audit_only
        or args.collection is None
        or not all(value is not None for value in telegram_args)
    ):
        parser.error("Telegram evidence requires --audit-only, --collection and all four Telegram arguments")
    try:
        matrix = load_matrix()
        if args.preflight:
            report = preflight()
            _print(report)
            return 0 if report["valid"] else 2
        if args.negative_control:
            _print(negative_controls())
            return 0
        if args.plan:
            busy = exclusive_slot_busy()
            payload = plan_commands(args.plan)
            payload["exclusive_slot_busy"] = busy
            payload["go_emitted"] = False
            _print(payload)
            return 0
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tools.quality_gate_inventory import InventoryError, load_collection, load_inventory

        try:
            nodeids = load_collection(args.collection) if args.collection is not None else None
        except InventoryError as exc:
            raise AcceptanceError("case_collection_invalid") from exc
        bindings = audit_case_bindings(matrix, nodeids)
        execution = {"status": "NOT_RUN", "executed_nodes": 0, "case_layers": {}, "go_emitted": False}
        native_execution = {
            "status": "NOT_RUN",
            "case_layers": {},
            "case_statuses": {},
            "root_failure": None,
            "go_emitted": False,
        }
        app_soak_execution = {
            "status": "NOT_RUN",
            "case_layers": {},
            "case_statuses": {},
            "root_failure": None,
            "go_emitted": False,
        }
        telegram_execution = {
            "status": "NOT_RUN",
            "case_layers": {},
            "case_statuses": {"R10-LIVE-TELEGRAM-ROUNDTRIP": "NOT_RUN"},
            "root_failure": None,
            "go_emitted": False,
            "full_gate_credit": False,
        }
        if (
            args.gate_receipt is not None
            or args.native_receipt is not None
            or args.app_soak_receipt is not None
            or args.telegram_receipt is not None
        ):
            try:
                inventory = load_inventory(ROOT / "tools/quality_gate_inventory.tsv")
            except InventoryError as exc:
                raise AcceptanceError("gate_inventory_invalid") from exc
        if args.gate_receipt is not None:
            identity = _frozen_gate_identity(args.candidate_sha, args.base_sha, args.wheel_sha256, inventory)
            if matrix != load_matrix():
                raise AcceptanceError("gate_candidate_not_frozen")
            execution = audit_gate_execution(
                matrix,
                nodeids,
                receipt_path=args.gate_receipt,
                receipt_sha256=args.gate_receipt_sha256,
                expected_identity=identity,
                inventory=inventory,
            )
        if args.native_receipt is not None:
            try:
                context_in_run = args.native_context.resolve().is_relative_to(
                    args.native_receipt.parent.resolve()
                )
            except (OSError, RuntimeError) as exc:
                raise AcceptanceError("native_context_invalid") from exc
            if context_in_run:
                raise AcceptanceError("native_context_not_independent")
            native_context = _read_native_context(args.native_context, args.native_context_sha256)
            if not set(native_context["case_ids"]).issubset(bindings["verified_case_ids"]):
                raise AcceptanceError("native_case_binding_invalid")
            native_identity = _frozen_native_identity(native_context, inventory)
            if matrix != load_matrix():
                raise AcceptanceError("native_candidate_not_frozen")
            if args.gate_receipt is not None and (
                any(
                    identity[key] != native_identity[key]
                    for key in ("candidate_sha", "candidate_tree", "wheel_sha256")
                )
                or identity["base_sha"] != native_context["base_sha"]
            ):
                raise AcceptanceError("native_gate_identity_mismatch")
            native_execution = audit_native_execution(
                matrix,
                receipt_path=args.native_receipt,
                receipt_sha256=args.native_receipt_sha256,
                expected_identity=native_identity,
                expected_run_id=native_context["run_id"],
                expected_case_ids=native_context["case_ids"],
                expected_secondary=native_context["secondary_enabled"],
                expected_secondary_mode=native_context["secondary_mode"],
            )
        if args.app_soak_receipt is not None:
            from tools import release_1_0_soak_receipts as soak_receipts

            app_soak_context = soak_receipts.read_context(args.app_soak_context, args.app_soak_context_sha256)
            app_soak_expected = app_soak_context.get("candidate_identity", {})
            app_soak_identity = _frozen_gate_identity(
                app_soak_expected.get("candidate_sha"),
                app_soak_expected.get("base_sha"),
                app_soak_expected.get("wheel_sha256"),
                inventory,
            )
            app_soak_execution = soak_receipts.audit_app_soak_execution(
                receipt_path=args.app_soak_receipt,
                receipt_sha256=args.app_soak_receipt_sha256,
                context_path=args.app_soak_context,
                context_sha256=args.app_soak_context_sha256,
                expected_identity=app_soak_identity,
                expected_suite=matrix["revision"],
            )
        if args.telegram_receipt is not None:
            from tools import release_1_0_telegram_receipts as telegram_receipts

            telegram_context = telegram_receipts.read_context(
                args.telegram_context, args.telegram_context_sha256
            )
            telegram_expected = telegram_context.get("candidate_identity", {})
            telegram_identity = _frozen_gate_identity(
                telegram_expected.get("candidate_sha"),
                telegram_expected.get("base_sha"),
                telegram_expected.get("wheel_sha256"),
                inventory,
            )
            telegram_execution = telegram_receipts.audit_telegram_execution(
                receipt_path=args.telegram_receipt,
                receipt_sha256=args.telegram_receipt_sha256,
                context_path=args.telegram_context,
                context_sha256=args.telegram_context_sha256,
                expected_identity=telegram_identity,
                expected_suite=matrix["revision"],
            )
        case_layers = dict(execution["case_layers"])
        if set(case_layers) & set(native_execution["case_layers"]):
            raise AcceptanceError("native_gate_layer_conflict")
        case_layers.update(native_execution["case_layers"])
        if set(case_layers) & set(app_soak_execution["case_layers"]):
            raise AcceptanceError("app_soak_execution_layer_conflict")
        case_layers.update(app_soak_execution["case_layers"])
        if set(case_layers) & set(telegram_execution["case_layers"]):
            raise AcceptanceError("telegram_execution_layer_conflict")
        case_layers.update(telegram_execution["case_layers"])
        surfaces = discover_surfaces()
        classified = classify_surfaces(surfaces, matrix, verified_case_ids=bindings["verified_case_ids"])
        executed = classify_surfaces(
            surfaces,
            matrix,
            verified_case_ids=bindings["verified_case_ids"],
            executed_case_layers=case_layers,
        )
        sealed = audit_sealed_batteries()
        summary = matrix_summary(matrix, classified, sealed)
        summary["surface_counts"] = {key: len(value) for key, value in surfaces.items()}
        summary["classified"] = classified["classified"]
        summary["case_bindings"] = bindings
        summary["gate_execution"] = execution
        summary["native_execution"] = native_execution
        summary["app_soak_execution"] = app_soak_execution
        summary["telegram_execution"] = telegram_execution
        summary["surface_execution_gaps"] = executed["coverage_gaps"]
        summary["required_case_execution"] = [
            {
                "id": case["id"],
                "required_layer": case["layer"],
                "status": (
                    "PASS"
                    if case_layers.get(case["id"]) == case["layer"]
                    else app_soak_execution["case_statuses"][case["id"]]
                    if case["id"] in app_soak_execution["case_statuses"]
                    else telegram_execution["case_statuses"][case["id"]]
                    if case["id"] in telegram_execution["case_statuses"]
                    else "FAIL"
                    if native_execution["case_statuses"].get(case["id"]) == "FAIL"
                    else "NOT_RUN"
                ),
            }
            for case in matrix["cases"]
            if case["release_required"]
        ]
        summary["execution_complete"] = (
            native_execution.get("root_failure") is None
            and native_execution.get("evidence_valid", True) is True
            and not executed["unknown"]
            and not executed["coverage_gaps"]
            and all(row["status"] == "PASS" for row in summary["required_case_execution"])
        )
        summary["collection_sha256"] = _sha256_file(args.collection) if args.collection else None
        summary["complaints"] = list(classified["unknown"]) + [
            f"surface_without_case:{surface}" for surface in classified["coverage_gaps"]
        ]
        summary["complaints"].extend(bindings["complaints"])
        if sealed.get("valid") is not True:
            summary["complaints"] = list(summary["complaints"]) + list(sealed.get("complaints") or [])
        valid = sealed.get("valid") is True and not summary["complaints"]
        summary["valid"] = valid
        summary["valid_scope"] = "structural bindings and sealed inventory; not release execution"
        if args.gate_receipt is not None and (
            identity != _frozen_gate_identity(args.candidate_sha, args.base_sha, args.wheel_sha256, inventory)
            or matrix != load_matrix()
        ):
            raise AcceptanceError("gate_candidate_not_frozen")
        if args.native_receipt is not None and (
            native_context != _read_native_context(args.native_context, args.native_context_sha256)
            or native_identity != _frozen_native_identity(native_context, inventory)
            or matrix != load_matrix()
        ):
            raise AcceptanceError("native_candidate_not_frozen")
        if args.app_soak_receipt is not None and (
            app_soak_context
            != soak_receipts.read_context(args.app_soak_context, args.app_soak_context_sha256)
            or app_soak_identity
            != _frozen_gate_identity(
                app_soak_expected.get("candidate_sha"),
                app_soak_expected.get("base_sha"),
                app_soak_expected.get("wheel_sha256"),
                inventory,
            )
            or matrix != load_matrix()
        ):
            raise AcceptanceError("app_soak_candidate_not_frozen")
        if args.telegram_receipt is not None and (
            telegram_context
            != telegram_receipts.read_context(args.telegram_context, args.telegram_context_sha256)
            or telegram_identity
            != _frozen_gate_identity(
                telegram_expected.get("candidate_sha"),
                telegram_expected.get("base_sha"),
                telegram_expected.get("wheel_sha256"),
                inventory,
            )
            or matrix != load_matrix()
        ):
            raise AcceptanceError("telegram_candidate_not_frozen")
        _print(summary)
        return 0 if valid else 2
    except AcceptanceError as exc:
        _print({"schema": SCHEMA, "valid": False, "error": str(exc), "go_emitted": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
