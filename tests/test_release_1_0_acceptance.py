"""Harness contracts for the RC → 1.0 acceptance wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _acceptance():
    from tools import release_1_0_acceptance as acceptance

    return acceptance


def test_matrix_is_canonical_and_closed() -> None:
    acceptance = _acceptance()
    matrix = acceptance.load_matrix()
    ids = [item["id"] for item in matrix["capabilities"]]
    assert matrix["schema"] == acceptance.MATRIX_SCHEMA
    assert matrix["not_a_backlog"] is True
    assert len(ids) == len(set(ids))
    assert "CAP-FILES" in ids
    assert "CAP-GEMINI-PARITY" in ids
    cases = {item["id"]: item for item in matrix["cases"]}
    assert cases["R10-J01-UPLOAD-SEARCH-RESTART"]["executable"] is True
    assert cases["R10-LIVE-ANDROID"]["executable"] is False
    assert cases["R10-LIVE-DOC-WORD-FIRST-GEN"]["release_required"] is True
    assert cases["R10-LIVE-DOC-WORD-FIRST-GEN"]["expected_outcome"] == "pass"


def test_wrapper_cannot_emit_go_or_rewrite_sealed_manifests() -> None:
    acceptance = _acceptance()
    plan = acceptance.plan_commands("diagnostic-baseline")
    assert plan["go_emitted"] is False if "go_emitted" in plan else True
    text = json.dumps(plan)
    assert "emit GO" in text or "never prints GO" in plan["go_rule"]
    assert "rewrite sealed A/B manifests" in plan["cannot"]
    a = ROOT / "tests" / "fixtures" / "synthetic_live_battery_a.json"
    b = ROOT / "tests" / "fixtures" / "synthetic_live_battery_b.json"
    assert a.is_file() and b.is_file()


def test_negative_controls_turn_the_oracle_red() -> None:
    report = _acceptance().negative_controls()
    assert report["valid"] is True
    ids = {item["id"] for item in report["results"]}
    assert ids == {
        "R10-NEG-CTRL-WRONG-DIGEST",
        "R10-NEG-CTRL-EMPTY-COLLECTION",
        "R10-NEG-CTRL-FOREIGN-CANARY",
    }


def test_empty_collection_is_a_harness_error_not_pass() -> None:
    acceptance = _acceptance()
    with pytest.raises(acceptance.AcceptanceError, match="zero_collected_cases"):
        acceptance.evaluate_oracle({"expected_outcome": "pass"}, {"collected": False})


def test_audit_only_binds_sealed_batteries_and_does_not_touch_models() -> None:
    acceptance = _acceptance()
    sealed = acceptance.audit_sealed_batteries()
    assert sealed["valid"] is True
    assert sealed["pair"]["cases"] == 400
    assert sealed["acceptance"]["all"] == 160
    assert sealed["acceptance"]["focused"] == 120
    assert sealed["acceptance"]["p06"] == 40


def test_surface_scan_classifies_telegram_ui_and_cli() -> None:
    acceptance = _acceptance()
    matrix = acceptance.load_matrix()
    surfaces = acceptance.discover_surfaces()
    assert any(item == "telegram:chat" for item in surfaces["telegram"])
    assert any(item == "ui:inbox" for item in surfaces["ui"])
    assert any(item == "cli:backup" for item in surfaces["cli"])
    classified = acceptance.classify_surfaces(surfaces, matrix)
    assert classified["classified"] >= 80
    assert classified["unknown"] == []


def test_openapi_surface_is_fully_classified(settings) -> None:
    acceptance = _acceptance()
    matrix = acceptance.load_matrix()
    surfaces = acceptance.discover_surfaces(settings=settings)
    classified = acceptance.classify_surfaces(surfaces, matrix)
    assert classified["unknown"] == []
    assert classified["classified"] == sum(len(group) for group in surfaces.values())


def test_preflight_does_not_emit_secrets() -> None:
    report = _acceptance().preflight()
    blob = json.dumps(report)
    assert "secrets_emitted" in report
    assert report["secrets_emitted"] is False
    assert "api_token" not in blob.lower()
    assert "bearer" not in blob.lower()


def test_plan_final_keeps_red_a_from_starting_b(monkeypatch, capsys) -> None:
    import shlex

    from tools import release_1_0_deterministic as deterministic
    from tools import release_1_0_live_journeys as journeys
    from tools import release_1_0_native as native

    plan = _acceptance().plan_commands("final")
    diagnostic = _acceptance().plan_commands("diagnostic-baseline")
    for generated in (plan, diagnostic):
        assert any("B only if A is green" in item for item in generated["order"])
        live = generated["commands"]["live"]
        live_text = json.dumps(live)
        assert "tools/synthetic_live_battery.py" in live_text
        assert '--run-directory \\"$R10_B09_PAIR_ROOT\\"' in live_text
        assert '--b09-review-plan \\"$R10_B09_REVIEW_PLAN\\"' in live_text
        assert '--root-review-key \\"$R10_B09_ROOT_KEY\\"' in live_text
        assert '--b03-evidence \\"$R10_B09_PAIR_ROOT/battery-b/pass-03/' in live_text
        assert '--b09-evidence \\"$R10_B09_PAIR_ROOT/battery-b/pass-09/' in live_text
        preflight = 'test ! -e "$R10_B09_PAIR_ROOT" && test ! -L "$R10_B09_PAIR_ROOT"'
        battery_index = next(
            index for index, command in enumerate(live) if "tools/synthetic_live_battery.py" in command
        )
        task_index = next(
            index
            for index, command in enumerate(live)
            if "tools/synthetic_live_b09_evidence.py task" in command
        )
        acceptance_index = next(
            index for index, command in enumerate(live) if "tools/synthetic_live_acceptance.py" in command
        )
        assert live.index(preflight) < battery_index < task_index < acceptance_index
        review = generated["b09_final"]
        assert review["required"] is True
        assert review["closed_run_expected_exit"] == 4
        assert review["closed_run_is_acceptance"] is False
        assert review["review_semantics"] == "plan-bound B03/B09 source facts and rubric"
        assert review["outstanding_after_closed_run"] == [
            "sealed_review_task",
            "actual_preregistered_lab_review",
            "root_issue_bind_verify",
        ]
        assert review["actual_lab_review"] == "mandatory_preregistered_external_stage"
        assert review["root_binder_verify"] == "mandatory_exit_0"
        assert generated["coverage_denominators"]["official_ab"] == 400
        assert generated["coverage_denominators"]["canonical_live"] == 160
        assert [
            next(part for part in command.split() if part in {"issue", "bind", "verify"})
            for command in generated["commands"]["b09_post_review"]
        ] == ["issue", "bind", "verify"]
        assert [stage["id"] for stage in generated["stages"]] == [
            "harness",
            "exact_release",
            "live",
            "actual_content_review",
            "root_bind_and_verify",
            "telegram_deployment_device",
        ]
    assert plan["b09_final"]["mode_purpose"] == "final_evidence_only_root_owns_go"
    assert diagnostic["b09_final"]["mode_purpose"] == "diagnostic_only_no_release_go"
    live = plan["commands"]["live"]
    for relative in ("tools/synthetic_live_b09_evidence.py", "tools/synthetic_live_b09_lab.py"):
        assert relative in _acceptance().CANONICAL_TOOLS
        assert relative in native.SUITE_PATHS
        assert relative in deterministic.SUITE_PATHS
    assert "tools/quality_gate.py --tier exact-release" in " ".join(plan["commands"]["exact_release"])
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return {"status": "PASS", "go_emitted": False}

    monkeypatch.setattr(native, "run_native", execute)
    command = shlex.split(
        next(
            command for command in plan["commands"]["live"] if "tools/release_1_0_live_journeys.py" in command
        )
    )
    substitutions = {
        "$FRIDAY_ENV_FILE": "/var/tmp/plan-proof.env",
        "$candidate_sha": "1" * 40,
        "$evidence_dir/r10-native": "/var/tmp/plan-proof/r10-native",
    }
    arguments = [
        substitutions.get(token, token)
        for token in command[command.index("tools/release_1_0_live_journeys.py") + 1 :]
    ]
    assert journeys.main(arguments) == 0
    assert calls == [
        {
            "env_file": Path("/var/tmp/plan-proof.env"),
            "candidate_sha": "1" * 40,
            "run_dir": Path("/var/tmp/plan-proof/r10-native"),
        }
    ]
    assert json.loads(capsys.readouterr().out)["go_emitted"] is False
    harness = plan["commands"]["harness"]
    audit = next(command for command in harness if "tools/release_1_0_acceptance.py --audit-only" in command)
    assert '--collection "$r10_collection"' in audit
    collect_index = next(i for i, command in enumerate(harness) if "--inventory-collection" in command)
    assert collect_index < harness.index(audit)
    assert any(
        'tools/quality_gate_inventory.py --collection "$r10_collection" --check' in command
        for command in harness
    )
    tests = shlex.split(next(command for command in harness if "-m pytest" in command))
    assert set(tests[tests.index("-q") + 1 :]) == {
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_release_1_0_*.py")
    } | {
        "tests/test_quality_gate_deadlines.py",
        "tests/test_quality_gate_phase.py",
        "tests/test_quality_gate_bootstrap.py",
        "tests/test_synthetic_live_b09_evidence.py",
        "tests/test_release_1_0_token_oracles.py",
        "tests/test_release_1_0_reminder_oracles.py",
        "tests/test_release_1_0_user_oracles.py",
        "tests/test_release_1_0_account_oracles.py",
        "tests/test_release_1_0_identity_oracles.py",
        "tests/test_release_1_0_profile_oracles.py",
        "tests/test_release_1_0_chronicle_oracles.py",
        "tests/test_release_1_0_knowledge_read_oracles.py",
        "tests/test_release_1_0_knowledge_mutation_oracles.py",
        "tests/test_release_1_0_entity_queue_oracles.py",
        "tests/test_release_1_0_ui_oracles.py",
        "tests/test_release_1_0_ui_sources_chats_oracles.py",
        "tests/test_release_1_0_ui_three_oracles.py",
        "tests/test_release_1_0_data_source_oracles.py",
        "tests/test_release_1_0_audit_oracles.py",
        "tests/test_release_1_0_preset_oracles.py",
        "tests/test_quality_gate_process.py",
        "tests/test_graph_runtime_log_privacy.py::test_entity_audit_retains_no_content_or_content_hash_after_hard_purge",
        "tests/test_entity_edits_are_reversible.py::test_http_restore_is_self_service_and_audited",
        "tests/test_entity_edits_are_reversible.py::test_a_deleted_object_can_be_brought_back",
        "tests/test_entity_edits_are_reversible.py::test_restoring_an_entity_version_is_a_new_version_not_a_rewind",
        "tests/test_entity_edits_are_reversible.py::test_restore_does_not_cross_tenants",
        "tests/test_the_audit_log_never_takes_a_document_body.py::test_the_own_edit_and_delete_routes_cannot_copy_a_note_into_audit",
        "tests/test_shared_archive.py::test_one_person_finds_and_edits_what_another_wrote",
        "tests/test_users_can_forget_their_own_knowledge.py::test_a_plain_user_can_delete_their_own_knowledge_object",
        "tests/test_users_can_forget_their_own_knowledge.py::test_a_plain_user_still_cannot_delete_someone_elses_knowledge_object",
        "tests/test_eval_harness.py::test_eval_endpoints_end_to_end",
        "tests/test_eval_harness.py::test_metric_functions",
        "tests/test_eval_harness.py::test_add_list_delete_eval_case",
        "tests/test_eval_harness.py::test_run_eval_measures_and_detects_regression",
        "tests/test_eval_harness.py::test_run_eval_empty_gold_set",
        "tests/test_graph_policy_is_declared.py::test_admin_eval_search_declares_the_policy_and_writes_no_usage",
        "tests/test_retrieval_explain.py::test_retrieval_explain_endpoint",
        "tests/test_retrieval_explain.py::test_score_reconstructs_from_components",
        "tests/test_search_explain_contract.py::test_search_explain_api_is_privacy_safe_and_reports_unavailable_corpora",
        "tests/test_search_explain_contract.py::test_search_explain_api_rejects_unknown_corpus_and_date_role",
        "tests/test_the_compact_tab_has_something_to_show.py::test_the_list_is_empty_before_any_run",
        "tests/test_the_compact_tab_has_something_to_show.py::test_a_run_appears_in_the_list_and_reads_back",
        "tests/test_the_compact_tab_has_something_to_show.py::test_running_the_same_day_twice_makes_one_row",
        "tests/test_the_compact_tab_has_something_to_show.py::test_a_malformed_date_is_refused_not_guessed",
        "tests/test_the_compact_tab_has_something_to_show.py::test_someone_elses_compact_is_only_for_the_owner",
        "tests/test_the_compact_tab_has_something_to_show.py::test_the_list_carries_the_human_wording",
        "tests/test_executive.py::test_mission_http_endpoints_create_list_and_stop",
        "tests/test_mission_oversight_boundaries.py::test_reading_another_accounts_mission_is_recorded",
        "tests/test_mission_oversight_boundaries.py::test_a_delegated_admin_cannot_cancel_the_owners_mission",
        "tests/test_mission_oversight_boundaries.py::test_cancelling_an_ordinary_accounts_mission_still_works_and_is_recorded",
        "tests/test_the_export_says_what_it_is.py::test_body_free_response_does_not_advertise_a_nonexistent_plaintext_vault",
        "tests/test_file_delivery_privacy.py::test_admin_download_revalidates_immediately_before_atomic_read",
        "tests/test_the_export_says_what_it_is.py::test_explicit_full_owner_response_names_the_readable_vault",
        "tests/test_the_export_says_what_it_is.py::test_it_admits_what_it_leaves_behind",
        "tests/test_owner_mutation_boundaries.py::test_delegated_admin_cannot_export_owner_archive",
        "tests/test_export_private_reminder_isolation.py::test_export_keeps_dependencies_of_the_users_exact_private_alias",
        "tests/test_export_private_reminder_isolation.py::test_export_uses_current_and_historical_private_alias_identity_tokens",
        "tests/test_export_private_reminder_isolation.py::test_tenant_export_does_not_include_another_persons_monitor_query_or_chat",
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
        "tests/test_product_quality.py::test_admin_quality_workflows_and_api_ingest_default",
        "tests/test_relation_triage_in_chat.py::test_api_list_and_review_are_tenant_scoped_capability_gated_and_content_free",
        "tests/test_relation_triage_in_chat.py::test_review_is_idempotent_but_terminal_and_audit_is_content_free",
        "tests/test_relation_triage_in_chat.py::test_review_identity_comes_from_actor_not_request_body",
        "tests/test_relation_triage_in_chat.py::test_write_only_grant_cannot_read_or_review_relation_cards",
        "tests/test_conflict_queue_triage_hints.py::test_http_conflict_list_includes_triage_hint",
        "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[assessed]",
        "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[unless_explicit]",
        "tests/test_review_gate_is_uniform.py::test_pasted_text_follows_the_policy[always]",
        "tests/test_organs_importer.py::test_import_endpoint_queues_reviews_and_is_idempotent",
        "tests/test_organs_importer.py::test_import_endpoint_rejects_unknown_format_and_requires_auth",
        "tests/test_graph_runtime_log_privacy.py::test_resolution_and_merge_surfaces_never_publish_snapshots_or_evidence",
        "tests/test_resolution_queue_is_a_page.py::test_the_next_page_shows_what_the_first_one_hid",
        "tests/test_resolution_queue_is_a_page.py::test_the_queue_reports_the_whole_size_not_the_page_size",
        "tests/test_relations_actually_get_found.py::test_accepting_a_link_reconsiders_the_relations",
        "tests/test_relations_actually_get_found.py::test_rejecting_a_link_proposes_nothing",
        "tests/test_conflict_triage_in_chat.py::test_http_conflict_decide_dismisses_and_hides_from_suggested",
        "tests/test_admin_entity_mutation_http.py::test_admin_entity_create_persists_exact_target_row_and_first_version",
        "tests/test_admin_entity_mutation_http.py::test_admin_entity_patch_preserves_foreign_rows_and_appends_exact_revision",
        "tests/test_admin_entity_mutation_http.py::test_admin_entity_delete_records_tombstone_once_and_preserves_other_tenant",
        "tests/test_maturity_070.py::test_feedback_state_replaces_rating_and_updates_usage_attribution",
        "tests/test_maturity_070.py::test_current_feedback_stats_replace_superseded_signal",
        "tests/test_document_contour_observer_snapshot.py::test_http_snapshot_is_owner_only_and_numeric_loopback_only",
        "tests/test_production_read_only_observation_route.py::test_real_lifespan_collector_uses_the_existing_storage_connection",
        "tests/test_approval_reaches_the_person.py::test_the_route_executes_on_approval_and_only_once",
        "tests/test_approval_reaches_the_person.py::test_a_rejection_does_not_execute",
        "tests/test_approval_reaches_the_person.py::test_an_approval_that_cannot_execute_says_so",
        "tests/test_approval_reaches_the_person.py::test_the_chat_command_lists_what_waits_and_names_unknown_outcomes",
        "tests/test_approval_reaches_the_person.py::test_a_bystander_pressing_the_button_changes_nothing",
        "tests/test_one_operation_through_every_surface.py::test_a_stranger_sees_nothing_on_any_surface",
        "tests/test_containers_browse.py::test_http_tags_containers_and_filters",
        "tests/test_event_timeline.py::test_timeline_and_set_time_over_http",
        "tests/test_event_timeline.py::test_unified_timeline_page_is_exposed_over_http",
        "tests/test_containers_browse.py::test_create_container_validates_kind_parent_and_builds_part_of",
        "tests/test_containers_browse.py::test_container_knowledge_count_reflects_accepted_members",
        "tests/test_containers_browse.py::test_list_knowledge_tags_counts_casefold_and_excludes_deleted",
        "tests/test_event_timeline.py::test_set_event_time_validates_type_dates_and_range",
        "tests/test_event_timeline.py::test_timeline_page_unifies_events_and_relation_changes_under_one_limit",
        "tests/test_source_search.py::test_source_search_over_http_excludes_rejected_material",
        "tests/test_timeline_in_chat.py::test_the_period_total_comes_from_the_route_not_from_the_page",
        "tests/test_shared_archive.py::test_without_the_setting_isolation_is_intact",
        "tests/test_monitors_watch_a_topic.py::test_monitors_are_self_service_over_http",
        "tests/test_organs_reflection.py::test_reflection_endpoint_returns_digest_for_actor",
        "tests/test_monitors_watch_a_topic.py::test_a_foreign_monitor_cannot_be_stopped",
        "tests/test_monitors_watch_a_topic.py::test_a_person_cannot_hoard_monitors",
        "tests/test_organs_reflection.py::test_build_reflection_summarises_state",
        "tests/test_bridge_events.py::test_a_bridge_event_lands_in_the_journal",
        "tests/test_bridge_events.py::test_an_unknown_event_type_is_refused",
        "tests/test_bridge_events.py::test_the_endpoint_requires_bridge_authentication",
        "tests/test_bridge_events.py::test_the_payload_is_bounded_on_both_axes",
        "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.dead_letter]",
        "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.outbound_failed]",
        "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.outbound_recovered]",
        "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.poll_failed]",
        "tests/test_bridge_events.py::test_every_allowed_type_is_accepted[bridge.poll_recovered]",
    }
