# Friday: canonical project backlog

Updated: 2026-08-28

This is the project's only backlog and mutable status register. It owns the
current production identity, execution order, acceptance gaps and owner actions.
Architecture, operations and acceptance documents are immutable design inputs;
they must link here for live state and may not carry a competing task list.

Old agent task files, handoffs, dated reports and superseded status registers are
kept in Git history, not in the working tree. A task discovered anywhere else is
either merged here or discarded before that source is removed.

## Current production identity

- Branch: `main`
- Deployed implementation head: `b633ca7a1b3c8165c707c147b88112e58fca0379`
- Live: Friday `0.207.65` / `b633ca7a1b3c8165c707c147b88112e58fca0379`;
  tree `77fd335e5eb72b02084c5e3c22913040668f49aeb2bff8271346d5ef43610e6d`;
  wheel `739721a070cdd0aa94ed0917e050031058a1eda58498a58f36fed646a3dd720b`.
- Schema-capable fallback: Friday `0.207.65rc0` /
  `a69362c32a2d4b438a548966aa6f79091126e2ee`; runtime predecessor Friday
  `0.207.64` / `c78b8ca2ac959efdbe06aa0fe81d87270bcd35c0`.
- Database schema: 46
- Production: immutable activation `clear`; backend and Telegram bridge active;
  trusted-CA health `200`; SQLite integrity and foreign keys clean.
- Secondary: accepted/live GPT-OSS profile `gptoss20b-2335df…`; bounded
  document-map/current-document assist only. The primary remains sole tool,
  effect and final-publication owner; secondary absence preserves primary-only
  behavior.

## Active package

Finish and release the already-developed restart-safe `EngineerWorkItem v1`
continuation as the last journey-specific work store before the shared turn
contract. The package is deliberately narrow: fail closed to `UNKNOWN` on
command-ledger loss or rollback; bind code-owned source slots so dependent
same-message commands remain distinct; inject one authenticated observed
terminal receipt into one bounded `replan → next step or final` turn; publish
completion atomically with the Work Item CAS. Never persist prompt, reasoning,
argv, path or output bodies and never replay `RUNNING`/`UNKNOWN` blindly.

Acceptance remains a distinct schema-capable fallback, native full gate,
immutable rollback rehearsal and one benign signed owner smoke. After this
package, no new parallel work store may be added until the shared `TurnContext`
contract exists.

## Operating rules

- Release small independently reversible packages to `main` and production.
- Do not use Docker to certify primary Friday. The laptop inference node keeps
  its separate Docker contour.
- Do not touch the Obsidian companion plugin without a separate owner request.
- Preserve the primary-only path whenever the laptop or secondary runtime is
  unavailable.
- Measure progress by complete recoverable user journeys, not by another
  adapter, organ, evidence ceremony or store. One turn has one effect owner,
  one final publisher, one inherited deadline and one authorized source set.
- Secondary and Semantic Supervisor components are advisory. They may deepen
  analysis or propose a plan, but never own tools, effects or publication.
- Authorization, provenance, ingestion review, privacy, effect fencing,
  citation/coverage, cancellation, honest `UNKNOWN` and one-publication are
  product invariants; cognition profiles may never disable them.
- The owner-only autonomous Engineer Mode deliberately has no per-command HITL
  or isolated-workspace policy rail. Its hard boundary is instead fresh owner
  Telegram provenance and capability authorization; every other surface keeps
  its existing controls.
- Do not merge old feature branches wholesale. Re-audit and port only exact
  useful commits onto current `main`.

## Priority order

### P0 — production message stability

Status: deployed in `0.207.52`.

- Keep `AgentRuntime.chat` and every orchestration wrapper structurally
  signature-compatible.
- Preserve authenticated Telegram carrier identity through every legacy,
  fallback and canary branch.
- Keep signed `/api/chat` and signature-parity regressions release-blocking.

### P1 — make the current Engineer Mode a complete user workflow

Status: deployed in `0.207.57`.

Goal: close `authenticated owner request → autonomous plan/tool loop → host-user
run → progress → result files/archive` for arbitrary software installed in the
Friday VM. No `/approvals` callback is part of this mode. Friday chooses and
chains its own commands, sees their real output, may use the VM filesystem and
network as the Friday service user, and keeps durable cancellation/progress and
artifact delivery.

1. Preserve truthful trusted-output refusal receipts and independently port the
   isolated fix from commit `81998fd29b38adcace8dbfe717a4d74bed4d32f3`.
2. Read only sealed job outputs, revalidate path/type/size/SHA, persist them as
   generated Raw objects and deliver files or a deterministic archive exactly
   once through Telegram.
3. Add durable terminal notification and sparse fact-based progress after
   restart. Never invent percentages or ETA.
4. Resolve natural “status/cancel current task” through an exact
   actor/conversation binding; fail closed when more than one job is plausible.
5. Add bounded retention that never removes pending, uncertain or unpublished
   output.
6. Grant exact current-message Telegram files to a job as immutable read-only
   input snapshots; bind every digest to the request/receipt and re-authorize
   immediately before execution.
7. Wire the already-present bundle/publication seams to command, Java and patch
   flows so sources, binary/output and receipts arrive atomically.
8. Gate traversal, symlink, hardlink, race, tamper, restart, duplicate callback,
   cancellation/cgroup and nmap-route isolation; then perform one benign live
   Telegram smoke.

Items 1–2 and their traversal/symlink/hardlink/race/tamper/legacy/UNKNOWN
gates are deployed in `0.207.53`. Item 4 is deployed in `0.207.54` with durable
exact-scope focus and fail-closed ambiguity handling. Item 3 and the workspace
portion of item 5 are delivered in `0.207.56`: terminal publication is
automatic, progress is durable and fact-only, and only old proven-sent
workspaces are retired while canonical archives remain. The preceding
owner-confirmed isolated command admission is now a deployed predecessor, not
the target product contract. Autonomous owner-only host-user admission,
iterative planning, current Telegram inputs, lifecycle closure and items 6–8
are deployed in `0.207.57`. The live signed owner smoke executed an installed
command and returned exact stdout without an approval row. The P1 contour is
closed; current continuation belongs only to S1 and further toolchain expansion
is demand-driven.

Production regressions found after rollout are closed by `0.207.58`: composite
systemd time budgets are parsed exactly, current Telegram uploads reach the
autonomous service, an identical failed step executes once, model-authored fake
terminal messages are rejected, and terminal delivery no longer creates empty
archives. A real uploaded PE completed through the live command kernel with the
requested 300-second timeout. `0.207.59`/`0.207.60` restore stable V12 restart
attestation by widening only bounded SGLang observation budgets; same-epoch
identity, exact-zero drain and fail-closed semantics remain intact. Production
is `canary_ready` with `archive_read` and `file_read` live.

`0.207.62` removes model-selected deadlines from the arbitrary-command schema,
stops same-turn polling after durable admission, recovers terminal command truth
across provider failures and returns no-file stdout/stderr as bounded Telegram
text instead of an empty archive. Progress now exposes measured stage, elapsed
time, output byte counts and only a real hard-deadline remainder. `0.207.63`
deploys the reusable edited Telegram status surface and selective bounded
reasoning for complex plan/replan turns. Progress delivery is advisory and
cannot duplicate task execution; execution/status/final phases remain
no-thinking, and no approval rail was reintroduced.

P1B foundation is deployed dormant in `0.207.65`; remaining work is restart-safe
runtime continuation across messages. Activate one
journey-specific `EngineerWorkItem v1`, not a generic WorkGraph: persist only
owner/conversation/source identity, revision/state, code-owned step ordinal,
idempotency key and command/terminal receipt digests. A fresh authenticated
follow-up may inject the exact observed terminal receipt into one bounded
`replan → next step or final` turn. Never persist prompt/CoT/argv/output/path,
never blindly replay `RUNNING/UNKNOWN`, and publish completion atomically with
the Work Item CAS. Before activation, make the independent command ledger
fail-closed against loss/rollback and bind a code-owned source slot so dependent
same-message commands remain possible without replay ambiguity.

### S0 — freeze the constructor

Status: active architectural ratchet.

- Do not start another journey-specific store, generic WorkGraph, Host Control
  expansion, closed compiler catalog, MCP platform, sensitive-data plane,
  Decision store, material-quarantine path or heuristic-retirement system.
  Existing bug fixes remain allowed.
- Keep the shipped Supervisor foundation and accepted
  `gptoss20b-2335df…` profile; do not build a second release PKI, effect owner
  or post-commit observer around them.
- Replace the golden-journey validator's impossible unconditional trusted-root
  rejection with exact-release machine evidence from named executable tests
  and an authenticated owner Telegram smoke where an external edge exists.
  A mutable or self-declared receipt is never `VERIFIED`.
- Do not salvage stale feature branches wholesale.

### S1 — finish Engineer continuation

Status: active package; the implementation is already substantially complete.

- Close ledger-loss/rollback, source-slot, restart, publication and backup/
  restore invariants described in Active package.
- Build and retain a distinct schema-capable fallback, run the native gate,
  rehearse activation rollback and execute one benign signed owner smoke.
- Do not add a generic graph, model-authored persistence or a new approval rail.

### S2 — one turn nervous system

- Extend or wrap the existing `orchestration.contracts.TurnInput`; do not add a
  third unrelated DTO. Build one immutable authenticated turn contract after
  ingest/admission with actor, tenant, person, conversation, mode, turn ID,
  inherited deadline/budgets, authorized source identities, turn policy,
  effect fence and exactly one pending-work owner.
- Router, legacy runtime, Engineer, secondary and Supervisor consume the same
  contract. `AgentContext` is derived from it. Fallback may change strategy but
  may not discard the plan/source identities or classify the raw message again
  from zero.
- Carry one turn ID through ingest, route, tools and final publication. Land the
  current small-model behavior first with no user-visible change.

Estimate: 3–7 clean-work days in small reversible packages after S1.

### S3 — bounded second hemisphere

- Keep the primary as sole tool caller, effect owner and publisher. Secondary
  and Semantic Supervisor receive the shared turn contract and return bounded
  structured advice only; their absence is exactly the primary-only path.
- Let joined shadow observations accumulate from real eligible traffic; target
  20 for a useful sample, never fabricate traffic and never make the count a
  product-availability gate.
- Promote only the current-file-plus-current-public-web journey to bounded
  assist after release-bound evidence, latency evidence and assist-to-shadow
  rollback. Keep effect planning and heuristic retirement deferred.

Estimate: 8–16 hours after the minimal S2 seam.

### S4 — one search facade with passage memory

1. Add reader-first `document_passages` with a schema-capable fallback, bounded
   writer, restart-safe resumable backfill and honest `index_incomplete`.
2. Make `archive_search` reach parity with document, promoted-knowledge,
   bitemporal/graph and message semantics. Only after regression and shadow
   recall measurements may `memory_search`, `source_search` and
   `message_search` disappear from the dialogue model catalog; they remain
   internal adapters.
3. Add conversation passages and adjacent context as a separate reversible
   release.
4. Reconstruct a current measurable recall set before changing ranking or
   embeddings. Close filename, alias, format, date and truncation holes only
   from corpus-backed failures; do not revive `raw_fts.metadata_json`.

Estimate: 8–16 clean-work days across separately reversible releases.

### S5 — measured cognition and installation budgets

- Extend the existing attested `V12ModelProfileSpec`/lease instead of creating a
  competing environment-only model profile. Measured capabilities control
  lexical routing aids, verifier policy, history, tool rounds/calls, prepared
  evidence, native tools and context use.
- Preserve a baseline equivalent to today's 27–35B behavior. More capable
  leases may remove cognition crutches only after probes; authorization,
  provenance, review, privacy, citation/coverage, effect fencing and
  publication invariants never vary by model.
- Separate safety deadlines from model anti-loop limits and installation
  resource budgets. Every nested stage inherits the parent deadline.
- Remove the artificial V12 8k ceiling where the exact runtime is attested for
  40k; use `min(attested runtime, installation cap)` rather than a model-name
  guess.

Estimate: 4–8 clean-work days after S2, released incrementally.

### S6 — journey proof and recovery, not new organs

- Keep core Telegram, web, file/Office, Obsidian, Engineer and reminder paths
  release-blocking with named deterministic tests and exact-release evidence.
- Audit scheduled work for at-most-once delivery, restart/cancel/expiry and
  uncertain-effect recovery. Change the design only where a current journey is
  actually broken.
- Prove release-level clean-home backup/restore, deterministic index rebuild,
  ENOSPC, clock skew and duplicate inbound behavior without duplicating generic
  operator evidence into every journey row.
- Re-measure review queues before adding bounded aging/observability. Keep
  export-before-purge deferred until a real retention journey requires it.
- Perform Obsidian Android round-trip, reconnect and conflict acceptance only
  with the owner/device present; server-side work is otherwise complete and the
  companion remains excluded.

### Removed from the active queue

- Supervisor effect observation and heuristic retirement, Package 6 trusted
  attestation-root construction, closed compiler profiles, indexed Ghidra as a
  product, generic Active Frames/WorkGraphs, Executive unification, Host
  Capability growth, MCP generalization, sensitive-data/Decision stores and a
  material-quarantine release path have no current end-to-end product gap.
- Engineer's owner shell is the toolchain. Add an installed compiler, analyzer
  or decomposer only when a real request proves it absent; do not rebuild an
  allowlisted compiler zoo around arbitrary command execution.
- Existing dormant code is not authority to resume these items. Re-entry needs
  a current owner request or a corpus-backed broken journey.

## Explicitly deferred or closed

- Obsidian companion, shared/multi-device vault, desktop control and remote
  agents: deferred.
- Local coding-agent orchestrator: development tooling, not current Friday
  product convergence.
- Old WIP blobs, old V12 model-first branch and PLAN-002/004: superseded.
- Broad exploit validation: no work without separate scope and safe target.

## Owner/external actions

- Configure and verify an off-machine backup/file mirror; the implementation is
  present but an empty target leaves the installation without an offsite copy.
- Rotate the external web-search credential at its provider, then update the
  single protected runtime secret. Local duplicate cleanup did not revoke the
  old value.
- S6 physical Android acceptance requires the owner and device; all other work
  proceeds independently.

## First 24 clean-work hours

1. Keep the deployed P0/P1 production paths green.
2. Finish, certify, release and smoke `EngineerWorkItem v1` as one reversible
   package; do not expand its scope.
3. Define and land the smallest existing-`TurnInput`-based S2 seam, including a
   single turn ID and pending-work owner, before adding Supervisor behavior.
4. Begin the bounded S3 advisor path only through that seam. In parallel,
   replace the golden-registry trusted-root deadlock without accepting mutable
   or self-declared evidence.

## Canonical golden-journey/evidence registry

This is the single source of truth for product-level journey states. The
machine contract in `tests/test_golden_journey_registry.py` parses this table
directly; detailed trackers may link here but must not duplicate its states.

The readiness vocabulary is closed to `READY`, `DEGRADED`, `UNVERIFIED`,
`BLOCKED` and `OUT_OF_SCOPE`. Evidence is closed to `VERIFIED`, `AVAILABLE`,
`MISSING`, `STALE`, `FAILED` and `NOT_APPLICABLE`. `AVAILABLE` means that a
journey-specific contract, executable test or runbook exists, not that the
complete journey passed. Generic release, rollback and backup tests are not
journey evidence.

The grammar can represent `VERIFIED`, `FAILED` and `STALE`, but the current
validator still unconditionally rejects every manifest-backed claim with
`trusted_execution_attestation_unavailable`. That makes `READY`
mathematically unreachable and is an S0 defect, not a reason to build Package 6.
Replace the deadlock with a closed machine-produced receipt bound to the exact
commit, tree, wheel, schema and named executable tests, plus an authenticated
owner Telegram smoke where the journey crosses an external edge. A mutable,
generic or self-declared `PASSED`/`FAILED` receipt remains non-evidence.

The current validator performs structural preflight only. A manifest and its
sanitized receipt must use their single deterministic privacy-safe paths derived
from journey, class, result and the full release identity. They bind the exact
deployed source, tree, wheel and database schema, closed executable-test node IDs,
and SHA-256 digests of Git-blob source bytes at the manifest source commit, never
the mutable checkout. Closed allowlists forbid raw content, people,
conversations, prompts, responses, runtime paths, tool arguments, test bodies and
logs. `READY` requires every applicable journey class to be current `VERIFIED`
under that replacement rule; generic release/rollback/backup proof stays at
release level instead of being copied into every row. Obsidian remains
`UNVERIFIED` without current physical Android evidence, unless current `FAILED`
evidence makes it honestly `BLOCKED`. There are no decisive or `READY` claims at
this checkpoint.

| Journey ID | Journey | Readiness | deterministic contract | integration path | clean artifact path | synthetic live path | production read-only observation | physical device evidence | restart and recovery evidence | rollback evidence | backup and restore evidence | Limitation codes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `conversation_recall` | Conversation recall | `DEGRADED` | `AVAILABLE`<br>[friday/interaction_control_plane/work_item_contract.py](../friday/interaction_control_plane/work_item_contract.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py) | `AVAILABLE`<br>[friday/orchestration/message_window_outcome.py](../friday/orchestration/message_window_outcome.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update](../tests/test_message_window_work_item_runtime.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `semantic_recall_missing`<br>`cross_lane_coverage_missing` |
| `document_recall_answer` | Document recall and answer | `DEGRADED` | `AVAILABLE`<br>[friday/file_evidence_reader.py](../friday/file_evidence_reader.py)<br>[tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle](../tests/test_v12_file_evidence_reader.py) | `AVAILABLE`<br>[friday/orchestration/file_read.py](../friday/orchestration/file_read.py)<br>[tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections](../tests/test_v12_file_evidence_reader.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `AVAILABLE`<br>[tools/document_contour_live_battery.py](../tools/document_contour_live_battery.py)<br>[tests/test_document_contour_live_battery.py::test_manifest_is_exactly_ten_unique_document_scenarios](../tests/test_document_contour_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_archive_replay_failure_is_source_free_and_suspends](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `cross_lane_coverage_missing` |
| `obsidian_write_sync` | Obsidian write and synchronization | `UNVERIFIED` | `AVAILABLE`<br>[friday/organs/obsidian/contracts.py](../friday/organs/obsidian/contracts.py)<br>[friday/orchestration/effect_outcome.py](../friday/orchestration/effect_outcome.py)<br>[tests/test_effect_outcome.py::test_effect_outcome_is_immutable_canonical_closed_and_round_trips](../tests/test_effect_outcome.py)<br>[tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions](../tests/test_obsidian_structured_acceptance_core.py) | `AVAILABLE`<br>[friday/organs/obsidian/runtime.py](../friday/organs/obsidian/runtime.py)<br>[tests/test_agent_obsidian_acceptance_message_matrix.py::test_every_exact_tier_a_b_message_routes_through_full_chat_once](../tests/test_agent_obsidian_acceptance_message_matrix.py)<br>[tests/test_agent_obsidian_production_composition.py::test_note_create_append_and_daily_exact_messages_mutate_the_real_vault](../tests/test_agent_obsidian_production_composition.py) | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract](../tests/test_obsidian_syncthing_live.py) | `MISSING` | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text](../tests/test_obsidian_runtime.py)<br>[tests/test_obsidian_operations.py::test_unproved_append_stays_uncertain_and_never_mutates_the_vault](../tests/test_obsidian_operations.py) | `MISSING` | `MISSING` | `physical_android_round_trip_missing`<br>`real_conflict_evidence_missing` |
| `durable_scheduled_work` | Durable scheduled work | `UNVERIFIED` | `AVAILABLE`<br>[friday/reminder_schedule.py](../friday/reminder_schedule.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `AVAILABLE`<br>[friday/storage/_missions.py](../friday/storage/_missions.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_mission_budgets_and_recovery.py::test_spent_budget_survives_a_restart](../tests/test_mission_budgets_and_recovery.py)<br>[tests/test_mission_budgets_and_recovery.py::test_an_interrupted_side_effect_is_never_replayed_blindly](../tests/test_mission_budgets_and_recovery.py) | `MISSING` | `MISSING` | `current_code_journey_audit_missing`<br>`at_most_once_delivery_recovery_missing` |
| `honest_degradation` | Honest degradation | `DEGRADED` | `AVAILABLE`<br>[friday/orchestration/capability_outcome.py](../friday/orchestration/capability_outcome.py)<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result](../tests/test_search_provider_refusal_is_not_emptiness.py) | `AVAILABLE`<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses](../tests/test_search_provider_refusal_is_not_emptiness.py)<br>[tests/test_message_window_runtime_integration.py::test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried](../tests/test_message_window_runtime_integration.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_full_package_a_oracle_accepts_natural_honest_refusals](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution](../tests/test_message_window_work_item_runtime.py) | `MISSING` | `MISSING` | `product_multi_lane_coverage_missing`<br>`candidate_bound_fault_continuation_evidence_missing` |
| `current_file_web_comparison` | Current file and web comparison | `UNVERIFIED` | `AVAILABLE`<br>[tests/test_compare_current_file_web_work_graph_schema45.py::test_schema45_exact_binding_is_durable_immutable_and_revision_cas](../tests/test_compare_current_file_web_work_graph_schema45.py) | `AVAILABLE`<br>[tests/test_supervisor_assist_controller.py::test_review_and_web_recovery_are_strictly_bounded](../tests/test_supervisor_assist_controller.py) | `MISSING` | `MISSING` | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_supervisor_assist_graph_adapter.py::test_terminal_cancel_and_startup_reconcile_publish_closed_receipts](../tests/test_supervisor_assist_graph_adapter.py) | `MISSING` | `MISSING` | `assist_promotion_evidence_missing`<br>`clean_release_artifact_missing`<br>`activation_rollback_evidence_missing` |

## Update rule

After every production release update the source/live/fallback identities,
health, completed package, active package, evidence rows and next order here.
Never mark device-dependent or external-service observations complete from
local tests. No other tracked file may become a mutable backlog or status log.
