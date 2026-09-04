# ruff: noqa: E401,E501,E701,E702,I001
# fmt: off
import subprocess, pytest
from collections import Counter
from pathlib import Path
from tools import quality_gate_inventory as inventory
def test_checked_in_inventory_closes_semantics_tiers_parameters_and_host_boundaries() -> None:
    value = inventory.load_inventory()
    rules = {rule.function_id: rule for rule in value.rules}
    counts = Counter(rule.invariant_id for rule in value.rules)
    nightly = {rule.function_id for rule in value.rules if rule.tier == "nightly"}
    large = {"test_explicit_text_shape_regeneration.py", "test_full_document_hierarchical_contract.py", "test_live_acceptance_runtime_composition.py", "test_outside_deed_runtime_boundary.py"}
    assert inventory.canonical_inventory_bytes(value) == inventory.DEFAULT_INVENTORY_PATH.read_bytes()
    assert set(counts) == set(inventory.INVARIANT_CATALOG) and max(counts.values()) * 4 < len(value.rules)
    assert not any(word in key for key in counts for word in ("core", "default", "generic", "misc", "other"))
    assert sum(rule.scratch_mb for rule in value.rules if rule.tier != "nightly") == 15_390
    assert nightly == {"tests/retrieval_benchmark/test_conversation_harness.py::test_manifest_is_one_closed_six_by_four_conversation_matrix", "tests/retrieval_benchmark/test_conversation_harness.py::test_second_offline_run_is_byte_identical_and_never_uses_network", "tests/retrieval_benchmark/test_document_harness.py::test_document_manifest_is_one_closed_five_class_corpus", "tests/retrieval_benchmark/test_document_harness.py::test_second_offline_document_run_is_byte_identical_and_network_forbidden", "tests/retrieval_benchmark/test_harness.py::test_ephemeral_manifest_has_at_least_twenty_cases_and_all_ten_classes", "tests/retrieval_benchmark/test_harness.py::test_two_offline_real_path_runs_are_byte_identical", "tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract", "tests/test_schema_migration_chain.py::test_every_real_backup_migrates_and_opens", "tests/test_windows_gateway_publish_recovery.py::test_native_powershell_projection_passes"}
    assert sum(rule.tier == "change" and any(name in rule.function_id for name in large) for rule in value.rules) == 27
    expected = {
        "tests/test_auth_hardening.py::test_failed_auth_attempts_are_rate_limited_per_ip": "security.authentication-and-untrusted-input", "tests/test_backup_mirror.py::test_encrypted_mirror_roundtrip": "schema.migration-backup-and-restore",
        "tests/test_agent_obsidian_production_composition.py::test_note_create_append_and_daily_exact_messages_mutate_the_real_vault": "storage.transaction-and-lifecycle", "tests/test_keyboard_layout.py::test_digits_and_unmapped_characters_survive": "configuration.policy-version-compatibility",
        "tests/test_graph_snapshot_reaches_agent.py::test_entity_lookup_rejects_nonmapping_or_incomplete_graph_contract": "graph.entity-topology-and-provenance", "tests/test_graph_snapshot_reaches_agent.py::test_memory_search_rejects_nonmapping_or_incomplete_kg_fallback": "graph.entity-topology-and-provenance",
        "tests/test_graph_snapshot_reaches_agent.py::test_memory_search_rejects_nonmapping_or_incomplete_known_at_contracts": "graph.entity-topology-and-provenance", "tests/test_shared_nmap_integration.py::test_engineer_and_host_share_exact_nmap_argv_parser_and_projection": "host.process-tool-and-sandbox-containment",
        "tests/test_shared_nmap_integration.py::test_shared_nmap_version_probe_is_fixed_and_bounded": "host.process-tool-and-sandbox-containment", "tests/test_graph_path_invariants.py::test_query_context_refuses_a_relation_revision_racing_between_hops": "graph.entity-topology-and-provenance",
        "tests/test_secondary_brain.py::test_provisional_candidate_has_a_separate_shadow_only_admission": "configuration.policy-version-compatibility", "tests/test_runtime_events.py::test_events_round_trip_with_their_payload": "audit.logging-diagnostics-and-evidence", "tests/test_runtime_pinning.py::test_every_runtime_dependency_is_pinned": "configuration.policy-version-compatibility", "tests/test_graph_snapshot_reaches_agent.py::test_prepare_context_reuses_the_effective_graph_snapshot": "graph.entity-topology-and-provenance", "tests/test_the_feed_counts_files_by_their_author.py::test_files_without_an_author_are_counted_apart": "identity.people-relations-and-tenancy",
    }
    assert {name: rules[name].invariant_id for name in expected} == expected
    deterministic = ("tests/test_engineer_command_kernel.py::test_autonomous_issue_is_closed_to_model_shell_host_user_shape", "tests/test_engineer_command_kernel.py::test_committed_work_item_fence_wins_before_grant_parsing", "tests/test_engineer_command_progress_delivery.py::test_progress_worker_recovers_enqueue_before_private_cas", "tests/test_engineer_command_retention.py::test_pending_text_carrier_never_authorizes_workspace_retention", "tests/test_engineer_terminal_delivery.py::test_real_kernel_restart_unknown_is_proactively_published_without_reexecution", "tests/test_shared_nmap_integration.py::test_shared_nmap_version_probe_is_fixed_and_bounded")
    assert all((rules[name].tier, rules[name].execution_kind) == ("change", "unit") for name in deterministic)
    assert all((rules[name].tier, rules[name].execution_kind) == ("exact-release", "host-tool") for name in ("tests/test_engineer_command_kernel.py::test_argv_echo_completes_without_inheriting_caller_env", "tests/test_engineer_container_runtime.py::test_apparmor_profile_is_enforcing_grammar_not_an_escape_hatch", "tests/test_friday_host_agent_process_runner.py::test_capture_failure_after_start_is_reported_as_unknown", "tests/test_friday_host_agent_process_runner.py::test_direct_test_backend_treats_shell_metacharacters_as_literal_data", "tests/test_friday_host_agent_process_runner.py::test_output_timeout_and_cancel_are_bounded", "tests/test_friday_host_agent_process_runner.py::test_static_boundary_preflight_rejects_missing_bwrap_without_user_bus", "tests/test_friday_host_agent_process_runner.py::test_systemd_availability_fails_closed_when_effective_bwrap_probe_fails", "tests/test_friday_host_agent_process_runner.py::test_systemd_availability_smokes_both_exact_bwrap_profiles_once", "tests/test_friday_host_agent_process_runner.py::test_systemd_boundary_probe_negative_control_detects_visible_sibling_state"))
    large_exact = ("tests/test_engineer_compiler.py::test_real_compiler_bwrap_denies_extra_scratch_and_exhausts_only_bounded_tmpfs", "tests/test_engineer_container_runtime.py::test_real_sandbox_smoke_proves_network_namespace_and_connectivity", "tests/test_provision_document_toolchain.py::test_libreoffice_wrapper_has_a_real_clean_namespace_and_writable_workdir")
    assert all((rules[name].tier, rules[name].execution_kind) == ("exact-release", "host-tool") for name in large_exact)
    assert all(rules[name].max_runtime_s >= 300 and rules[name].scratch_mb >= 64 for name in large_exact)
    s6r4_modules = {"tests/test_production_read_only_observation.py": 13, "tests/test_production_read_only_observation_route.py": 8, "tests/test_production_read_only_observation_operator.py": 11, "tests/test_production_observation_exact_binding.py": 9}
    assert Counter(rule.module_path for rule in value.rules if rule.module_path in s6r4_modules) == Counter(s6r4_modules)
    assert all((rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s, rule.scratch_mb) == ("scheduling.worker-mission-and-supervision", "change", "unit", 300, 1) for rule in value.rules if rule.module_path == "tests/test_production_read_only_observation.py")
    assert all((rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s, rule.scratch_mb) == ("security.authentication-and-untrusted-input", "change", "unit", 300, 1) for rule in value.rules if rule.module_path == "tests/test_production_read_only_observation_route.py")
    assert all((rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s, rule.scratch_mb) == ("external.live-system-observation", "change", "unit", 300, 8) for rule in value.rules if rule.module_path == "tests/test_production_read_only_observation_operator.py")
    assert all((rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s, rule.scratch_mb) == ("release.artifact-install-and-rollback", "exact-release", "candidate-artifact", 1800, 8) for rule in value.rules if rule.module_path == "tests/test_production_observation_exact_binding.py")
    p0h_modules = {"tests/test_release_artifact_proc_probe.py": 25, "tests/test_release_artifact_retention.py": 77, "tests/test_release_artifact_retention_maintenance.py": 24, "tests/test_release_artifact_retention_maintenance_install.py": 33, "tests/test_release_artifact_retention_maintenance_recovery.py": 28, "tests/test_release_artifact_retention_premount.py": 13, "tests/test_release_artifact_retention_privileged_install.py": 1, "tests/test_release_dr_generation_authentication.py": 7, "tests/test_release_dr_generation_enrollment.py": 18, "tests/test_release_dr_generation_index.py": 43, "tests/test_release_dr_generation_lifecycle.py": 13, "tests/test_release_dr_generation_rehearsal.py": 55}
    assert Counter(rule.module_path for rule in value.rules if rule.module_path in p0h_modules) == Counter(p0h_modules)
    p0h_immutable = {f"tests/test_immutable_release_operator.py::{name}" for name in (
        "test_build_parser_binds_all_manifest_digests_into_build_spec", "test_build_rejects_an_alternate_state_lock_scope_before_mutation", "test_build_rejects_every_noncanonical_home_layout_path_before_lock_mutation", "test_build_rejects_unadmitted_retention_toolchain_before_staging", "test_build_rejects_unknown_receipt_profile_before_staging", "test_build_uses_the_shared_release_operator_transaction_lock", "test_candidate_lock_scope_rejects_a_different_canonical_state_dir", "test_cli_never_constructs_a_mutating_activation_port_before_the_lock", "test_engineer_backup_byte_reauthentication_does_not_mutate_backup_namespace", "test_mutable_regular_file_primitives_reject_fifo_substitution_bounded", "test_operator_release_layout_binds_exact_root_and_sealed_unit_paths", "test_operator_transaction_creation_fsync_failure_releases_global_lock", "test_operator_transaction_guard_pins_named_state_inode_through_mutation", "test_operator_transaction_guard_pins_portable_runtime_root_inode", "test_operator_transaction_lock_serializes_the_fixed_systemd_unit_pair_across_homes", "test_operator_transaction_lock_survives_state_directory_replacement", "test_operator_transaction_rejects_preplanted_runtime_root_symlink", "test_operator_transaction_runtime_domain_cannot_split_when_run_user_appears", "test_operator_transaction_unit_pair_uses_filesystem_not_abstract_socket_namespace", "test_operator_transaction_uses_one_portable_bounded_global_lock_domain", "test_reader_only_release_profile_revalidates_bundle_and_omits_receipt_pairs", "test_recovery_journal_probe_never_creates_a_missing_backup_root", "test_recovery_keeps_exact_legacy_fallback_exempt_from_new_candidate_layout", "test_release_retention_toolchain_absolute_entrypoints_import_sealed_closure", "test_release_retention_toolchain_admission_revalidates_closed_bundle", "test_release_retention_toolchain_receipt_pair_fails_closed", "test_release_retention_toolchain_receipt_pair_preserves_historical_v1", "test_release_tree_copy_verifies_against_its_authenticated_bind_mount_path", "test_runtime_layout_preserves_the_in_state_legacy_database_name", "test_runtime_rejects_every_noncanonical_home_layout_path_before_port_construction", "test_two_homes_cannot_reuse_one_release_root_under_independent_build_locks",
    )}
    p0h = tuple(rule for rule in value.rules if rule.module_path in p0h_modules or rule.function_id in p0h_immutable)
    assert len(p0h) == 368 and sum(rule.node_count for rule in p0h) == 528 and sum(rule.scratch_mb for rule in p0h) == 625
    p0h_host = {"tests/test_release_artifact_proc_probe.py::test_repeated_mount_cache_hits_do_not_leak_task_root_descriptors", "tests/test_release_artifact_proc_probe.py::test_same_euid_snapshot_uses_stable_process_identity_not_volatile_cpu_counters", "tests/test_release_artifact_proc_probe.py::test_unprivileged_complete_snapshot_fails_closed_on_nondumpable_same_uid", "tests/test_release_artifact_proc_probe.py::test_unshared_worker_fd_and_cwd_are_seen_when_leader_holds_neither", "tests/test_release_artifact_retention_privileged_install.py::test_privileged_probe_installer_is_atomic_exact_and_rollback_safe", "tests/test_release_dr_generation_rehearsal.py::test_installed_bwrap_accepts_pinned_fd_mounts_without_close_fd", "tests/test_release_dr_generation_rehearsal.py::test_real_bwrap_hides_host_tools_cwd_tmp_and_network"}
    p0h_install_host = {rule.function_id for rule in p0h if rule.module_path == "tests/test_release_artifact_retention_maintenance_install.py"}
    assert len(p0h_install_host) == 33
    p0h_host |= p0h_install_host
    p0h_candidate = "tests/test_immutable_release_operator.py::test_release_retention_toolchain_absolute_entrypoints_import_sealed_closure"
    assert {rule.function_id for rule in p0h if rule.execution_kind == "host-tool"} == p0h_host
    assert all((rules[name].invariant_id, rules[name].tier, rules[name].execution_kind, rules[name].max_runtime_s) == ("host.process-tool-and-sandbox-containment", "exact-release", "host-tool", 300) for name in p0h_host)
    assert (rules[p0h_candidate].invariant_id, rules[p0h_candidate].tier, rules[p0h_candidate].execution_kind, rules[p0h_candidate].max_runtime_s, rules[p0h_candidate].scratch_mb) == ("release.artifact-install-and-rollback", "exact-release", "candidate-artifact", 1800, 8)
    assert all((rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s) == ("release.artifact-install-and-rollback", "change", "unit", 300) for rule in p0h if rule.function_id not in p0h_host | {p0h_candidate})
def test_parser_and_classifier_reject_function_parameter_and_canonical_drift(tmp_path: Path) -> None:
    nodes = ("tests/test_alpha.py::test_matrix[a]", "tests/test_alpha.py::test_matrix[b]")
    rule = inventory.FunctionRule(
        "tests/test_alpha.py::test_matrix", "security.authentication-and-untrusted-input",
        "change", "unit", 30, 2, 2, inventory.nodeids_sha256(nodes),
    )
    value = inventory.GateInventory((rule,))
    raw = inventory.canonical_inventory_bytes(value)
    assert inventory.parse_inventory_bytes(raw) == value
    assert tuple(item.nodeid for item in value.classify(tuple(reversed(nodes)))) == tuple(reversed(nodes))
    for bad in ((nodes[0],), (*nodes, nodes[1]), (nodes[0], "tests/test_alpha.py::test_matrix[c]"),
                (*nodes, "tests/test_alpha.py::test_new")):
        with pytest.raises(inventory.InventoryError):
            value.classify(tuple(bad))
    for bad in (raw.replace(b"\n", b"\r\n"), raw.rstrip(), raw + b"\n",
                raw.replace(b"\tchange\t", b"\tdefault\t"), raw.replace(b"\t30\t", b"\t030\t")):
        with pytest.raises(inventory.InventoryError):
            inventory.parse_inventory_bytes(bad)
    target, collection = tmp_path / "inventory.tsv", tmp_path / "collection.json"; target.write_bytes(raw)
    def collect(*exact: str) -> None: collection.write_bytes(inventory.json.dumps({"version": 1, "nodeids": exact}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    collection.write_bytes(b'{"nodeids":[],"nodeids":[],"version":1}')
    with pytest.raises(inventory.InventoryError): inventory.load_collection(collection)
    drift = (nodes[0], "tests/test_alpha.py::test_matrix[c]"); collect(*drift)
    with pytest.raises(SystemExit): inventory.main(["--collection", str(collection), "--inventory", str(target), "--check"])
    assert inventory.main(["--collection", str(collection), "--inventory", str(target), "--write"]) == 0
    assert inventory.load_inventory(target).rules == (inventory.FunctionRule(rule.function_id, rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s, rule.scratch_mb, 2, inventory.nodeids_sha256(drift)),)
    assert inventory.main(["--collection", str(collection), "--inventory", str(target), "--check"]) == 0
    with pytest.raises(SystemExit): inventory.main(["--collection", str(collection), "--inventory", str(target), "--check", "--declare", rule.function_id, rule.invariant_id, rule.tier, rule.execution_kind, "30", "2"])
    new = "tests/test_beta.py::test_new"; collect(*drift, new)
    with pytest.raises(SystemExit): inventory.main(["--collection", str(collection), "--inventory", str(target), "--write"])
    declaration = (new, "configuration.policy-version-compatibility", "change", "unit", "30", "0")
    with pytest.raises(SystemExit): inventory.main(["--collection", str(collection), "--inventory", str(target), "--write", "--declare", *declaration, "--declare", *declaration])
    assert inventory.main(["--collection", str(collection), "--inventory", str(target), "--write", "--declare", *declaration]) == 0
    assert inventory.load_inventory(target).rules[-1].invariant_id == declaration[1]
    collect(new)
    with pytest.raises(SystemExit): inventory.main(["--collection", str(collection), "--inventory", str(target), "--check"])
def test_candidate_binding_uses_pinned_git_and_exact_regular_module_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    oid = b"a" * 40
    tree = (b"100644 blob " + oid + b"\ttests/test_alpha.py\0"
            b"100755 blob " + oid + b"\ttests/nested/test_beta.py\0")
    calls = []
    def run(argv, **kwargs):  # noqa: ANN001, ANN202
        calls.append((argv, kwargs))
        stdout = b"commit\n" if "cat-file" in argv else tree
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/poison")
    monkeypatch.setattr(inventory.subprocess, "run", run)
    rules = tuple(inventory.FunctionRule(f"{path}::test_one", "security.authentication-and-untrusted-input", "change", "unit", 30, 0, 1, inventory.nodeids_sha256((f"{path}::test_one",))) for path in ("tests/test_alpha.py", "tests/nested/test_beta.py"))
    assert inventory.GateInventory(rules).validate_candidate_modules(tmp_path, "1" * 40) == tuple(sorted(("tests/test_alpha.py", "tests/nested/test_beta.py")))
    assert all(call[0][0] == "/usr/bin/git" and "GIT_OBJECT_DIRECTORY" not in call[1]["env"] for call in calls)
    tree = b"120000 blob " + oid + b"\ttests/test_alpha.py\0"
    with pytest.raises(inventory.InventoryError):
        inventory.candidate_test_modules(tmp_path, "1" * 40)
