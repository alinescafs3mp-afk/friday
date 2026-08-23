# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-23
- Branch: `main`
- Deployed implementation head: `3d2bef322c8069d2a5f8a708d59094e6a6ac0eb3`
- Live: Friday `0.207.7` / `3d2bef322c8069d2a5f8a708d59094e6a6ac0eb3`;
  tree `b363b73ce155a706b85c4fa2dfd8eb9d81839b3c48eca5f79c341109158aa8ba`;
  wheel `61a446ebf3973455320752952530936161a47939cbbd5986c2cdb995697c71e8`
- Schema-capable fallback: Friday `0.207.6` /
  `61cb15fa70aa8c0e23eab7dbd2dbaebf92882ace`;
  tree `1402a4c389e6fbb509df3621afbbacd78189c9f9a12a3bd199c4d2ae64fbbcc5`
- Database schema: 38
- Production state: immutable activation `clear`; backend and Telegram bridge
  active; trusted-CA HTTPS health `200`; SQLite integrity and FK checks clean
- Delivery constraints: no Docker; companion plugin untouched; small commits and
  immutable wheel-only production releases

## Active objective

Measure progress by complete, recoverable user journeys rather than isolated
adapters: define the journey/evidence registry, establish stable retrieval and
passage identity with honest coverage, expose one read-only archive facade, and
only then extend durable recall to documents. Keep `main` and the live release
healthy after every package.

## Convergence decision

`PROJECT_CONVERGENCE_ARCHITECT_BRIEF.md` is adopted as the current architectural
course, with these repository-specific constraints:

- The schema-38 canary stays deliberately narrow. It will not pre-bake generic
  WorkGraph, candidate-set, `SourceRef` or `PassageRef` columns before their
  contracts are proved.
- The next unit of completion is a golden user journey with named evidence
  classes and an honest readiness state, not another locally green component.
- Retrieval identity is a rebuildable projection over authoritative stores;
  it never replaces their lifecycle or authorization decisions.
- `archive_search` is initially a read-only facade over existing specialized
  lanes. Partial, stale, capped, incompatible and unavailable coverage must be
  visible and can never support a confident archive-absence claim.
- The release-evidence package should extend the immutable operator's existing
  journals/manifests instead of creating a competing release path.
- Android/Syncthing, restore and external-service claims remain `UNVERIFIED`
  until direct evidence exists. Docker and companion-plugin work remain out of
  the current contour.

## Completed and deployed

### Obsidian core

- Server-side Tier A/Tier B acceptance contour is implemented: create, read,
  append/prepend/replace, daily notes, tasks, properties/tags, search,
  continuations, links/backlinks, move/delete, templates, BaseSpec, recovery,
  conflict handling and honest synchronization state.
- User-visible Markdown is markerless. Operation identity and arguments are kept
  in the private receipt store, not in note bodies.
- The optional companion plugin has not been changed and is not a dependency.
- Detailed tracker: `docs/OBSIDIAN_IMPLEMENTATION_TRACKER.md`.

### Interaction Control Plane foundation

- P0A privacy-safe immutable TurnTrace and durable assistant-side publication
  tracing are deployed.
- P0B schema-37 precommit failure traces, bounded retention/deletion and the
  privacy-safe admin episode baseline are deployed.
- P2 schema-38 `RecallConversation` is deployed: a typed exact message-window
  request creates a privacy-safe durable Work Item, and an immediate closed
  temporal follow-up updates only its bounded window while retaining authorized
  owner, conversation, role and timezone identity. Expiry, cancellation,
  revision-CAS, restart, receipt/plan binding and atomic rollback are covered;
  unresolved references fail closed without a model or judge fallback.
- Detailed status: `outer_sol/INTERACTION_CONTROL_PLANE_IMPLEMENTATION_STATUS.md`.

### Typed read outcomes and durable receipts

- `FILE_READ` and `ARCHIVE_READ`: typed CapabilityOutcome, deterministic
  completion gate and atomic private accepted-outcome receipt.
- `search_explain`: bounded privacy-safe retrieval diagnostics.
- `498aa1b`: pinned `source_search` numeric continuation now publishes through a
  typed `ARCHIVE_READ` outcome and receipt.
- `272b64c`: isolated public-news legacy lane now publishes through a typed
  `WEB_READ` outcome and receipt without widening general web behavior.
- `4c02ab8`: exact current-conversation time-window reads now use one bounded
  storage snapshot, fresh authorization, deterministic citations, final
  same-transaction re-attestation and an atomic typed receipt. The lane never
  falls through to the model, judge or an alternate source on failure.
- Detailed V12 status: `outer_sol/V12_FURTHER_REFINEMENT_STATUS.md`.

## In progress

1. Publish the implemented read-only federated `archive_search` through the
   runtime with private exact-byte carriers and same-transaction late
   reauthorization.
2. Keep every lane capability-scoped, late-reauthorized and explicit about
   partial, capped, stale, incompatible and unavailable coverage.

## Completed and deployed (current package)

### Retrieval identity and coverage foundation (`0.207.5`)

- `SourceRef` separates stable logical identity from mutable `ResolvedSource`;
  tenant/principal lookup axes, representation/lifecycle/revision matrices and
  every revalidation target are closed and exact.
- `PassageRef` anchors codepoint spans or message windows to exact source
  revisions. Schema-38 embedding identity never treats stale, incompatible,
  missing or backfill-pending material as current.
- `TemporalFact` preserves exact roles and provenance; legacy collapsed dates
  and Knowledge Object projection dates cannot silently substitute source dates.
- `CatalogItem` is body-free, rebuildable and non-authoritative. Every catalog,
  passage, lexical, approximate-identity and dense lane reports an explicit
  index state.
- `SearchCoverage` binds each per-lane result to one keyed private request,
  authority scope, requested target set, snapshot and run. Mixed batches fail
  closed and only complete, current, reauthorized coverage can confirm absence.
- Independent schema/privacy review passed; 52 focused adversarial tests and the
  complete 16-worker quality gate are green. No database migration or runtime
  route was introduced.
- The wheel was reproduced byte-for-byte, activated through the immutable
  operator and accepted by exact backend/bridge process-root, trusted-CA health,
  SQLite integrity/FK and schema-38 checks.

### Federated retrieval foundation and document hotfixes (`0.207.6`–`0.207.7`)

- The read-only archive service, authority contract, stable source/passage
  identity and honest per-lane coverage are implemented. Runtime publication is
  deliberately still closed until its private exact-byte carrier and final
  same-transaction reauthorization are complete.
- Current-upload replay now uses the full Office document candidate matrix and
  a process-owned capability pinned to the source identity. Local attachment
  answers no longer inherit stale web-search authorization, while fresh web
  requests remain fail-closed.
- Rootless Tesseract OCR (`rus+eng`) and sealed rootless LibreOffice conversion
  are provisioned without Docker or system-package mutation. Common OOXML/ODF,
  legacy DOC/XLS/PPT, StarOffice XML and the LibreOffice registry's supported
  uncommon Office families enter the deterministic extraction contour;
  MIME-only RTF and neutral-carrier Office MIME types are covered.
- Archive lexical reads no longer bypass lifecycle/authorization through the
  global `raw_fts`; unavailable lifecycle-filtered derivatives report honest
  `BACKFILL_PENDING`/partial coverage instead.
- The scoped Office/OCR/replay/search/security gate passed 1,198 tests from a
  clean temporary Friday home. Ruff, mypy, compileall and diff checks are clean.
  Registry support is not a promise to recover corrupt, encrypted or malformed
  third-party files.
- `0.207.7` was reproduced byte-for-byte and activated `clear`; backend and
  Telegram bridge run from the exact candidate root, HTTPS health reports
  `ok`/`0.207.7`, SQLite quick-check and foreign keys are clean, schema remains
  38, and both local document toolchains are discoverable by the live runtime.
- During validation, an inherited production database path reached a test
  process. Execution was stopped, the verified immutable `0.207.6` pre-test
  snapshot was restored through the standard backup operator, and post-restore
  identity, counts, integrity, FTS and services were verified with no real
  message loss. Subsequent gates explicitly unset that path and use an isolated
  temporary Friday home.

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

The grammar can represent `VERIFIED`, `FAILED` and `STALE`, but Package 1 has no
trusted machine runner, signing key or attestation root. The validator therefore
rejects every manifest-backed claim with
`trusted_execution_attestation_unavailable`; a self-declared `PASSED` or
`FAILED` receipt is not evidence. Package 6 must add a machine-produced trusted
attestation before any of these states can be used.

The current validator performs structural preflight only. A manifest and its
sanitized receipt must use their single deterministic privacy-safe paths derived
from journey, class, result and the full release identity. They bind the exact
deployed source, tree, wheel and database schema, closed executable-test node IDs,
and SHA-256 digests of Git-blob source bytes at the manifest source commit, never
the mutable checkout. Closed allowlists forbid raw content, people,
conversations, prompts, responses, runtime paths, tool arguments, test bodies and
logs. `READY` requires every applicable class to be current `VERIFIED`. Obsidian
remains `UNVERIFIED` without current physical Android evidence, unless current
`FAILED` evidence makes it honestly `BLOCKED`. There are no decisive or `READY`
claims at this checkpoint.

| Journey ID | Journey | Readiness | deterministic contract | integration path | clean artifact path | synthetic live path | production read-only observation | physical device evidence | restart and recovery evidence | rollback evidence | backup and restore evidence | Limitation codes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `conversation_recall` | Conversation recall | `DEGRADED` | `AVAILABLE`<br>[friday/interaction_control_plane/work_item_contract.py](../friday/interaction_control_plane/work_item_contract.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py) | `AVAILABLE`<br>[friday/orchestration/message_window_outcome.py](../friday/orchestration/message_window_outcome.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py) | `MISSING` | `MISSING` | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update](../tests/test_message_window_work_item_runtime.py) | `MISSING` | `MISSING` | `semantic_recall_missing`<br>`cross_lane_coverage_missing` |
| `document_recall_answer` | Document recall and answer | `DEGRADED` | `AVAILABLE`<br>[friday/file_evidence_reader.py](../friday/file_evidence_reader.py)<br>[tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle](../tests/test_v12_file_evidence_reader.py) | `AVAILABLE`<br>[friday/orchestration/file_read.py](../friday/orchestration/file_read.py)<br>[tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections](../tests/test_v12_file_evidence_reader.py) | `MISSING` | `AVAILABLE`<br>[tools/document_contour_live_battery.py](../tools/document_contour_live_battery.py)<br>[tests/test_document_contour_live_battery.py::test_manifest_is_exactly_ten_unique_document_scenarios](../tests/test_document_contour_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `MISSING` | `MISSING` | `MISSING` | `stable_source_passage_identity_missing`<br>`cross_lane_coverage_missing`<br>`durable_continuation_missing` |
| `obsidian_write_sync` | Obsidian write and synchronization | `UNVERIFIED` | `AVAILABLE`<br>[friday/organs/obsidian/contracts.py](../friday/organs/obsidian/contracts.py)<br>[tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions](../tests/test_obsidian_structured_acceptance_core.py) | `AVAILABLE`<br>[friday/organs/obsidian/runtime.py](../friday/organs/obsidian/runtime.py)<br>[tests/test_agent_obsidian_acceptance_message_matrix.py::test_every_exact_tier_a_b_message_routes_through_full_chat_once](../tests/test_agent_obsidian_acceptance_message_matrix.py) | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract](../tests/test_obsidian_syncthing_live.py) | `MISSING` | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text](../tests/test_obsidian_runtime.py) | `MISSING` | `MISSING` | `common_effect_envelope_missing`<br>`uncertain_effect_reconciliation_missing`<br>`physical_android_round_trip_missing`<br>`real_conflict_evidence_missing` |
| `durable_scheduled_work` | Durable scheduled work | `UNVERIFIED` | `AVAILABLE`<br>[friday/reminder_schedule.py](../friday/reminder_schedule.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `AVAILABLE`<br>[friday/storage/_missions.py](../friday/storage/_missions.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_mission_budgets_and_recovery.py::test_spent_budget_survives_a_restart](../tests/test_mission_budgets_and_recovery.py)<br>[tests/test_mission_budgets_and_recovery.py::test_an_interrupted_side_effect_is_never_replayed_blindly](../tests/test_mission_budgets_and_recovery.py) | `MISSING` | `MISSING` | `current_code_journey_audit_missing`<br>`at_most_once_delivery_recovery_missing` |
| `honest_degradation` | Honest degradation | `DEGRADED` | `AVAILABLE`<br>[friday/orchestration/capability_outcome.py](../friday/orchestration/capability_outcome.py)<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result](../tests/test_search_provider_refusal_is_not_emptiness.py) | `AVAILABLE`<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses](../tests/test_search_provider_refusal_is_not_emptiness.py)<br>[tests/test_message_window_runtime_integration.py::test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried](../tests/test_message_window_runtime_integration.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_full_package_a_oracle_accepts_natural_honest_refusals](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution](../tests/test_message_window_work_item_runtime.py) | `MISSING` | `MISSING` | `product_multi_lane_coverage_missing`<br>`candidate_bound_fault_continuation_evidence_missing` |

## Current cumulative gate

- 16,235 non-UI Python tests passed on 16 workers with no failures or skips.
- The pinned Syncthing `v2.1.3` managed-REST smoke passed; this is not evidence
  of an Android round trip.
- Schema-38 fixture, lifecycle/privacy, store adversarial, runtime continuation,
  migration-chain and existing named-inventory compatibility checks are green.
- Ruff, release-surface format (884 files), mypy (217 source files), compileall,
  Bandit HIGH, JavaScript syntax and toolchain preflight passed.
- The release wheel was built twice from the clean commit archive and matched
  byte-for-byte. Immutable activation completed `clear`; schema-38 fallback,
  database/inbox backups, trusted-CA health, exact process roots, SQLite
  quick-check, foreign keys and Work Item DDL were verified.
- The newer `0.207.7` scoped regression gate adds 1,198 clean Office, OCR,
  current-upload, archive, web-search and security scenarios; its immutable
  wheel and current production process roots match the identities above.

## Next order

1. Release one read-only federated `archive_search` facade with deterministic
   continuation, neighboring message context and explicit per-lane coverage.
2. Extend the proven recall Work Item across document and message evidence only
   through stable source/passage references and fresh authorization/revision
   checks.
3. Add one uncertainty-aware common effect envelope and prove one idempotent,
   receipt-backed Obsidian mutation/reconciliation vertical.
4. Extend existing immutable-release evidence into machine-reconcilable
   source/wheel/schema/activation/fallback manifests, then register exact-release
   journey evidence without promoting generic component gates.
5. Run actual Android/Syncthing, backup/clean-restore and bounded recovery/fault
   certification; simulations remain labelled separately.
6. Evaluate generic WorkGraph, broader effects, connectors or companion work
   only from the golden journeys still failing after the preceding packages.

## Open source/document issue

The operator-referenced
`outer_sol/DOCUMENT_FILE_CONTOUR_WIP_AUDIT_2026-08-22.md` is not present in this
repository or local workspace at the current checkpoint. Its recommendations
must not be claimed as reviewed until the source is recovered or supplied.

## Update rule

After each released package, update at least: source/live/previous identities,
production health, completed package, current gate and next ordered item. Do not
mark device-dependent or external-service observations complete from local
tests.
