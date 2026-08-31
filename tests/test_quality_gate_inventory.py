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
    assert sum(rule.scratch_mb for rule in value.rules if rule.tier != "nightly") == 14_355
    assert nightly == {"tests/retrieval_benchmark/test_conversation_harness.py::test_manifest_is_one_closed_six_by_four_conversation_matrix", "tests/retrieval_benchmark/test_conversation_harness.py::test_second_offline_run_is_byte_identical_and_never_uses_network", "tests/retrieval_benchmark/test_harness.py::test_ephemeral_manifest_has_at_least_twenty_cases_and_all_ten_classes", "tests/retrieval_benchmark/test_harness.py::test_two_offline_real_path_runs_are_byte_identical", "tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract", "tests/test_schema_migration_chain.py::test_every_real_backup_migrates_and_opens", "tests/test_windows_gateway_publish_recovery.py::test_native_powershell_projection_passes"}
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
