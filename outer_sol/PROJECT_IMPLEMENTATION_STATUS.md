# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-24
- Branch: `main`
- Secondary implementation checkpoint: `1e3834dd5d987f84c6ca6a490c0cd9b3ac2756ed`
  (provisional exact-finalist certification; the 0.207.10 release closure is
  pending)
- Deployed implementation head: `2b197e1e467e93a085a1b4cc330fbda8b5b7b982`
- Live: Friday `0.207.9` / `2b197e1e467e93a085a1b4cc330fbda8b5b7b982`;
  tree `c3e689c7291c4919df60df163ec786208bb5f15d24c5a28813f551729ef7b6c0`;
  wheel `cd8ab7320718cce4c7d15caef1f8ee36e4d2c25c063578d02980bf59fcc4af48`
- Schema-capable fallback: Friday `0.207.9` /
  `f1426ca561f8914574cebf3a69f8dde83f79b568`;
  tree `eb8102ccf759b0f2a2d9a0a38584d9cda0c4938f14d389ac55246d87e536e6f7`;
  wheel `cd8ab7320718cce4c7d15caef1f8ee36e4d2c25c063578d02980bf59fcc4af48`
- Database schema: 39
- Production state: immutable activation `clear`; backend and Telegram bridge
  active; trusted-CA HTTPS health `200`; SQLite integrity and FK checks clean
- Delivery constraints: no Docker for primary Friday release certification;
  companion plugin untouched; small commits and immutable wheel-only production
  releases. The optional laptop inference node is a separate Docker contour.

## Active objective

Build the optional detachable GPT-OSS-20B/SGLang secondary node described in
`OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`, preserving
exact primary-only behavior whenever that node is absent. The narrow schema-39
archive-evidence continuation is deployed in `0.207.9`; broader Interaction
Control Plane work remains paused until the urgent secondary-brain package.

The measured provisional finalist is exact profile
`gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`
with candidate-manifest SHA-256
`51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439`:
native MXFP4/BF16, BF16 KV, 4,096-token total context, 512-token output,
`mem_fraction_static=0.96`, prefill chunk 256, page one, radix/overlap and hybrid
SWA 0.80 enabled, full decode CUDA graph at batch one and prefill graph off.
The fresh epoch-D warm capacity-v2 trial passed seven non-streaming exact
512-token repeats at runtime epoch `1787601267.06`; receipt SHA-256 is
`b317e964eced1c0a80d5d8f4cc7fcb388d60598c16dfbeb9f320f1076fa97719`,
median end-to-end completion rate is `108.497563` tokens/s and minimum free GPU
memory is 1,294 MiB. The accepted-profile registry remains empty, deployment is
default-off, production sends no traffic to the laptop and `assist` is blocked.
Fresh `soak.epoch-d.full.7c1f742.json` is active; cold-restart capacity-v2 and
acceptance remain pending. Older passed soak and streaming capacity-v1 receipts
are historical only and cannot satisfy v2 acceptance.

Detailed active tracker:
`outer_sol/OPTIONAL_SECONDARY_BRAIN_IMPLEMENTATION_STATUS.md`.

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
  until direct evidence exists. Docker remains outside primary Friday
  certification; the separate laptop inference node uses its existing Docker
  Desktop installation. Companion-plugin work remains out of scope.

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

1. Finish the fresh exact-finalist epoch-D 30-minute soak, then run the matching
   fail-fast capacity-v2 protocol after a cold restart without promoting an
   in-progress or historical receipt.
2. Release the exact current finalist code default-off, then complete
   controlled and physical laptop-loss evidence from that sealed source.
3. Register only the accepted manifest in a separate default-off release, then
   prove private product shadow before bounded assist with exact primary
   fallback.
4. Resume `INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`; extend durable
   recall only through the deployed source/passage, coverage and
   late-reauthorization contracts.

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

### Authorized federated archive search (`0.207.8`)

- The runtime now exposes one read-only `archive_search` facade across current
  authorized documents, Knowledge Objects, accepted owner messages and the
  exact configured Obsidian vault. Explicit corpus selection and all-corpus
  union both retain deterministic source labels and per-corpus coverage.
- Invocation and result carriers are process-private and exact-byte bound.
  Authority, preset, capability, source revision and coverage are rechecked in
  the final database transaction; drift, cancellation, replay or late denial
  produces a source-free failure and cannot publish archive claims.
- Archive absence is code-owned and requires complete current coverage. Partial,
  capped, stale, incompatible, unavailable and backfill-pending lanes cannot be
  presented as an empty archive.
- Current private archive requests remain isolated from public web search.
  Negative, reported and pasted archive commands do not spuriously route, while
  ordinary punctuation, Unicode and technical identifiers remain accepted.
- Document intake now rejects arbitrary renamed legacy-DOC bytes, treats
  malformed Pages as unreadable rather than unsupported, keeps PDF page labels
  out of the nested-JSON quarantine, and reports the actual final OCR outcome.
- The deterministic wheel was built twice byte-for-byte, activated `clear`, and
  accepted by exact process-root, trusted-CA HTTPS `ok`/`0.207.8`, schema-38,
  SQLite quick-check, foreign-key and clean post-cutover journal gates.

### Durable archive evidence and default-off secondary foundation (`0.207.9`)

- Schema 39 deploys the narrow body-free selected-archive-evidence sidecar and
  restart continuation with fresh source, revision and authority checks.
- The optional private-CA secondary control plane is deployed default-off. The
  live `0.207.9` registry contains the superseded provisional
  `gptoss20b-ce6c00ff988e35c97d7381bde47cfa56f6e89c3eeb879bf6e7ba5e0b4a9d81e3`;
  the current exact finalist is still unreleased. Both accepted registries are
  empty and `assist` fails closed.
- Live identity is `2b197e1e467e93a085a1b4cc330fbda8b5b7b982`, with the separate
  schema-39 fallback `f1426ca561f8914574cebf3a69f8dde83f79b568`. Backend and bridge
  are active and trusted-CA health reports `ok`/`0.207.9`.

## Document-contour WIP audit disposition

The recovered 0.207.4 inventory was revalidated against current `main`; no old
blob or migration is a release candidate:

- embedding backlog/freshness observability is already stronger in diagnostics,
  tenant-scoped search explain and worker indexing than the stale global
  `embeddings_pending` proposal;
- filename and alias search is already authority-scoped in owned-file lookup and
  the archive catalog lane; indexing arbitrary `metadata_json` in `raw_fts` is
  rejected as a privacy, migration and WAL regression;
- the useful message-layout recall and synthetic P09 parenthetical/adverse
  predicates were already absorbed and strengthened by `acda581`; the archived
  aggregate recall test is weaker than the current focused contracts;
- five clean merged feature worktrees, 23 clean ancestor worktrees carrying
  false `pending` locks, three obsolete gate caches and stale missing-worktree
  registrations were removed. Current `main`, live immutable releases,
  rollback/evidence roots and companion code were untouched;
- ambiguous forensic refs (`document-file-contour-wip`, `de37c64` and G45) remain
  retained until their separate semantic/retention review; cleanup did not trade
  recoverability for disk space.

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

- The latest completed full non-UI gate before the soak-protocol-only follow-up
  commits passed 17,951 tests; one explicitly configured real-Syncthing case
  remained environment-gated. Static and focused secondary gates are green.
- The `0.207.9` wheel reproduced byte-for-byte. Immutable activation completed
  `clear`; exact candidate and schema-39 fallback identities, recovery receipts,
  trusted-CA health, process roots, schema 39, SQLite quick-check and foreign
  keys passed.
- No warning-or-higher backend or bridge journal entries were emitted during
  the post-cutover verification window.

## Next order

1. Complete the active fresh epoch-D 30-minute soak, matching cold-restart
   capacity-v2 and failure certification of the exact provisional native-MXFP4
   finalist.
2. Release its exact current code default-off and close the sealed-source
   controlled/physical failure evidence.
3. Register only its accepted manifest in a separate default-off release; then
   prove private product shadow before bounded assist, including laptop-off and
   disconnect-mid-turn.
4. Resume broader ICP work from the deployed schema-39 archive-evidence
   vertical.
5. Add durable candidate selection/pending-question state only when it preserves
   exact archive coverage and survives restart without persisting model prose.
6. Add one uncertainty-aware common effect envelope and prove one idempotent,
   receipt-backed Obsidian mutation/reconciliation vertical.
7. Extend existing immutable-release evidence into machine-reconcilable
   source/wheel/schema/activation/fallback manifests, then register exact-release
   journey evidence without promoting generic component gates.
8. Run actual Android/Syncthing, backup/clean-restore and bounded recovery/fault
   certification; simulations remain labelled separately.
9. Evaluate generic WorkGraph, broader effects, connectors or companion work
   only from the golden journeys still failing after the preceding packages.

## WIP source retention

`DOCUMENT_FILE_CONTOUR_WIP_AUDIT_2026-08-22.md` was recovered at
`/home/jericho/DOCUMENT_FILE_CONTOUR_WIP_AUDIT_2026-08-22.md`, outside the
repository location named by the operator. Its recommendations are now
reconciled above; the external source and compact forensic refs remain retained
until an explicit evidence-retention decision.

## Update rule

After each released package, update at least: source/live/previous identities,
production health, completed package, current gate and next ordered item. Do not
mark device-dependent or external-service observations complete from local
tests.
