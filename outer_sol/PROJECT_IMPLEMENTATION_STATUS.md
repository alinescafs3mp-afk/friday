# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-25
- Branch: `main`
- Secondary acceptance base: Friday `0.207.15` is live in provisional public
  shadow. The accepted-profile registry remains empty pending physical
  acceptance; private text and `assist` remain closed.
- Deployed implementation head: `8f260ce05bc9ad7384df9780e2383c727b9ab35d`
- Live: Friday `0.207.15` / `8f260ce05bc9ad7384df9780e2383c727b9ab35d`;
  tree `b538ac0f07a72ee89a1837b62f318db236f56203f7f7aa91f420b38be1fd4ec0`;
  wheel `5031ec389c95607555188a85f043641ccae85ecb32fef85e992c33891454c09b`
- Immediate predecessor and schema-capable fallback: Friday `0.207.14` /
  `cce33d5daef12fa4ae239e4b3d891a0a4d907c93`, tree
  `d8b2d67dfe2099c900546e2157d451163fcace1e6befa3195966fb576b8cc5f2`
- Database schema: 40
- Production state: immutable activation `clear`; backend and Telegram bridge
  active; trusted-CA HTTPS health `200`; SQLite integrity and FK checks clean;
  exact provisional profile is configured in public `shadow/extract`, while
  primary remains final and no private material is eligible
- Delivery constraints: no Docker for primary Friday release certification;
  companion plugin untouched; small commits and immutable wheel-only production
  releases. The optional laptop inference node is a separate Docker contour.

## Active objective

Finish the optional detachable GPT-OSS-20B/SGLang node through physical
acceptance, private shadow and bounded assist, preserving exact primary-only
behavior whenever the laptop is absent. The provisional public-shadow cutover
is complete. Its causal request witness is deployed and the exact deterministic
and controlled-live evidence was rebuilt from that release. Work requiring a
physical laptop power cut waits for the owner. The schema-40 ICP candidate
runtime is live. Durable DocumentCatalog/enrichment is the active autonomous
package; the common effect envelope and V12 refinement follow it.

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
memory is 1,294 MiB. Epoch-D soak then passed 1,800.218 seconds / 4,467 requests /
zero failures with 1,294 MiB minimum free GPU memory, 75 C peak and SHA-256
`852673984f6705c148d0a92957d3c2f2fd5360925b0fddd2225eb8b631a8983a`.
Cold epoch `1787603294.09` passed the matching capacity-v2 protocol 7/7 at exact
512 tokens, median `106.733375` end-to-end tokens/s, 1,296 MiB minimum free and
SHA-256 `9c60611b939098020faa4f9077debde3bec96c9ded2bffc3c3385fc94d5ffa87`.
The verified v2 capacity wrapper hashes to
`519b5912428f491dc65928c5ba2d2e33a6408566fe5f3496501ce2e760b9205e`;
the superseded operational v1 assertion produced no false success. The
accepted-profile registry remains empty. Production now performs exact-profile
startup/admission probes and permits only non-private discarded
`shadow/extract`; `assist` is blocked.

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
- Schema 40 now serves immutable body-free ordered archive candidates and a
  durable ordinal question. Strict RU/EN ordinal replies replay the exact
  authorized source/revision after restart without another search or model
  call; expiry, cancellation, drift, replacement and races fail closed.
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

1. Implement the narrow durable DocumentCatalog/enrichment projection without
   persisting source bodies or model prose.
2. When the owner is present, perform the real causal laptop cut/on and accept
   only the exact bound finalist; then promote distinct private-shadow/assist
   candidates with product-linked fallback evidence.

## Completed and deployed (current package)

### Durable archive candidate runtime (`0.207.15`)

- An archive answer with 2–20 distinct replayable sources creates one exact
  body-free choice. A strict RU/EN ordinal resumes the selected source and
  revision without a second search or model call.
- Admission is bound before attachment ingestion to exact
  user/conversation/work-item/revision identity. Restart, expiry, cancellation,
  mode/source/authority drift, stale receipts, replacement and CAS races fail
  closed; `стоп` always wins.
- The exact source passed 18,209 non-UI and 31 UI tests with real pinned
  Syncthing 2.1.3. Two clean wheels reproduced byte-for-byte; immutable
  activation completed `clear`, health is `ok`, both services are active and
  schema 40 integrity/FK/FTS checks are clean.

### Schema-40 durable candidate foundation (`0.207.14`)

- Immutable body-free ordered archive candidate sets and ordinal questions are
  migration/restart ready; the prompt remains deliberately dormant until the
  separately audited runtime package is released.
- A distinct code-identical schema-40 rollback twin was sealed before migration.
  Activation completed `clear`; backend/bridge are active, optional secondary
  public shadow remains healthy, and SQLite integrity/FK/FTS checks are clean.
- The exact source passed 18,164 non-UI and 31 UI tests, including real pinned
  Syncthing 2.1.3; two clean wheels reproduced byte-for-byte.

### Causal physical-loss witness (`0.207.13`)

- `physical-causal-request` signals the operator only after the complete
  authenticated request body has reached the pinned-CA TLS endpoint. Physical
  acceptance now requires that receipt and cannot pass on a manual assertion.
- The receipt binds exact source, runner, candidate, process epoch and CA while
  retaining no prompt, response, key, exception text, tool or effect material.
- The primary continuity probe is explicitly separate from the later product
  scheduler-fallback proof. The exact release passed 18,074 non-UI and 31 UI
  tests, reproduced its wheel byte-for-byte and activated `clear` on schema 39.
- Exact-release deterministic evidence passed 101 cases (SHA-256
  `7a66c9a02628f0cc31c0ddfb33f52220386dcfe268d5873292189689abf0fc8b`);
  controlled gateway/runtime loss and recovery passed (SHA-256
  `4d344b3d810ebb0e2bb4e7af3c5750f3bdc79a8ed54b55bb1b6a59570440d395`).
  Neither receipt claims a physical power loss.

### General public-web execution hotfix (`0.207.12`)

- Natural Russian and English public-news/search requests now enter one typed
  current-message-only `WEB_READ`, preserve exact calendar and freshness
  semantics and cannot inherit stale private file history.
- File, note, CRM, person, secret, quoted and compound-effect carriers remain
  fail-closed; Unicode/confusable and cross-feature regression matrices passed.
- The exact immutable source passed 18,067 non-UI and 31 UI tests, reproduced
  its wheel byte-for-byte and is live with schema 39, clean SQLite integrity/FK
  checks and both services active.

### GPT-OSS provisional public shadow (`0.207.12`)

- The exact finalist passed a fresh trusted-TLS endpoint probe, the 101-case
  deterministic failure battery and controlled gateway/runtime restart from
  deployed source `7913c633`; raw content and credentials were not retained.
- A distinct immutable activation published public-only `shadow/extract` at
  `a6e3c09`; health and diagnostics bind the exact profile with
  `profile_admission=provisional_shadow`. Output is discarded and primary owns
  every answer, tool and effect.
- Physical power-loss evidence, accepted registration, private shadow and
  assist remain unclaimed.

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
  `0.207.9` registry contained the superseded provisional
  `gptoss20b-ce6c00ff988e35c97d7381bde47cfa56f6e89c3eeb879bf6e7ba5e0b4a9d81e3`;
  the current exact finalist was not yet released. Its accepted registry was
  empty and `assist` failed closed.
- Release identity was `2b197e1e467e93a085a1b4cc330fbda8b5b7b982`, with the separate
  schema-39 fallback `f1426ca561f8914574cebf3a69f8dde83f79b568`. Backend and bridge
  were accepted with trusted-CA health `ok`/`0.207.9`.

### Exact provisional finalist released default-off (`0.207.10`)

- Source/released identity is `aaae455a3eec6024c1e4e338d8f00b31ee90f995`,
  tree `4f5c5e9a130e33f47fbf8f9282362f77b18b8f625d00f313b0cda4124d7ab76e`
  and wheel
  `a563ad94c678ca5332f0cfe142ef65a18c6cc4a12f7e07b9d64c2734d06181f6`.
- The exact finalist is released default-off with an empty accepted registry;
  production sends no secondary traffic and `assist` remains closed.
- Protocol, post-cold probe, quality 29/29, warm/cold capacity-v2 and the
  30-minute soak are exact. Capacity v2 is accepted; failure/profile acceptance
  and profile registration remain pending.

### Secondary rollout boundary released default-off (`0.207.11`)

- Source is `0c985cf41ee01e6beb2187134f42ff8dd8088deb`; immutable release,
  trusted-CA health, schema 39, SQLite integrity and foreign keys passed.
- The sealed product runner and one-use rollout receipt consumption boundary
  are deployed. The accepted profile registry remains empty, so production
  still sends no traffic to GPT-OSS and `assist` remains closed pending physical
  and product-stage evidence.

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
- `archive/de37c64-wip-20260822` and `g45-pdf-scan-live-repair` are proven
  code-superseded and may be removed after a retained forensic bundle is
  verified;
- `refs/archive/document-file-contour-wip-20260822` remains held until its 824
  unique evidence blobs are bundled or explicitly discarded. Product code in
  that ref is superseded, but the evidence is not otherwise retained.

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
| `conversation_recall` | Conversation recall | `DEGRADED` | `AVAILABLE`<br>[friday/interaction_control_plane/work_item_contract.py](../friday/interaction_control_plane/work_item_contract.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py) | `AVAILABLE`<br>[friday/orchestration/message_window_outcome.py](../friday/orchestration/message_window_outcome.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update](../tests/test_message_window_work_item_runtime.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `semantic_recall_missing`<br>`cross_lane_coverage_missing` |
| `document_recall_answer` | Document recall and answer | `DEGRADED` | `AVAILABLE`<br>[friday/file_evidence_reader.py](../friday/file_evidence_reader.py)<br>[tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle](../tests/test_v12_file_evidence_reader.py) | `AVAILABLE`<br>[friday/orchestration/file_read.py](../friday/orchestration/file_read.py)<br>[tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections](../tests/test_v12_file_evidence_reader.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `AVAILABLE`<br>[tools/document_contour_live_battery.py](../tools/document_contour_live_battery.py)<br>[tests/test_document_contour_live_battery.py::test_manifest_is_exactly_ten_unique_document_scenarios](../tests/test_document_contour_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_archive_replay_failure_is_source_free_and_suspends](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `cross_lane_coverage_missing` |
| `obsidian_write_sync` | Obsidian write and synchronization | `UNVERIFIED` | `AVAILABLE`<br>[friday/organs/obsidian/contracts.py](../friday/organs/obsidian/contracts.py)<br>[tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions](../tests/test_obsidian_structured_acceptance_core.py) | `AVAILABLE`<br>[friday/organs/obsidian/runtime.py](../friday/organs/obsidian/runtime.py)<br>[tests/test_agent_obsidian_acceptance_message_matrix.py::test_every_exact_tier_a_b_message_routes_through_full_chat_once](../tests/test_agent_obsidian_acceptance_message_matrix.py) | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract](../tests/test_obsidian_syncthing_live.py) | `MISSING` | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text](../tests/test_obsidian_runtime.py) | `MISSING` | `MISSING` | `common_effect_envelope_missing`<br>`uncertain_effect_reconciliation_missing`<br>`physical_android_round_trip_missing`<br>`real_conflict_evidence_missing` |
| `durable_scheduled_work` | Durable scheduled work | `UNVERIFIED` | `AVAILABLE`<br>[friday/reminder_schedule.py](../friday/reminder_schedule.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `AVAILABLE`<br>[friday/storage/_missions.py](../friday/storage/_missions.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_mission_budgets_and_recovery.py::test_spent_budget_survives_a_restart](../tests/test_mission_budgets_and_recovery.py)<br>[tests/test_mission_budgets_and_recovery.py::test_an_interrupted_side_effect_is_never_replayed_blindly](../tests/test_mission_budgets_and_recovery.py) | `MISSING` | `MISSING` | `current_code_journey_audit_missing`<br>`at_most_once_delivery_recovery_missing` |
| `honest_degradation` | Honest degradation | `DEGRADED` | `AVAILABLE`<br>[friday/orchestration/capability_outcome.py](../friday/orchestration/capability_outcome.py)<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result](../tests/test_search_provider_refusal_is_not_emptiness.py) | `AVAILABLE`<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses](../tests/test_search_provider_refusal_is_not_emptiness.py)<br>[tests/test_message_window_runtime_integration.py::test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried](../tests/test_message_window_runtime_integration.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_full_package_a_oracle_accepts_natural_honest_refusals](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution](../tests/test_message_window_work_item_runtime.py) | `MISSING` | `MISSING` | `product_multi_lane_coverage_missing`<br>`candidate_bound_fault_continuation_evidence_missing` |

## Current cumulative gate

- The exact `0.207.15` canonical gate passed 18,209 non-UI tests plus 31 UI
  tests, including the pinned Syncthing 2.1.3 smoke; static checks and focused
  causal-secondary gates are green.
- Friday `0.207.15` public shadow is live at the exact source/tree/wheel identity
  above, schema 40, with `cce33d5` (`0.207.14`) as its schema-capable fallback.
  Immutable activation completed `clear`.

## Next order

1. Build the durable body-free DocumentCatalog/enrichment sidecar and expose
   honest current/backfill-pending archive coverage.
2. Complete the owner-observed causal cut/on when the owner is present; only then
   accept/register and promote distinct private-shadow/assist candidates.
3. Add one uncertainty-aware common effect envelope and prove one idempotent,
   receipt-backed Obsidian mutation/reconciliation vertical.
4. Extend existing immutable-release evidence into machine-reconcilable
   source/wheel/schema/activation/fallback manifests, then register exact-release
   journey evidence without promoting generic component gates.
5. Run actual Android/Syncthing, backup/clean-restore and bounded recovery/fault
   certification; simulations remain labelled separately.
6. Begin V12 refinement with `ExplainSelectedArchiveEvidence`, and make the
   cross-feature search/file/Obsidian/dialogue battery mandatory for releases.
7. Evaluate generic WorkGraph, broader effects or connectors only from the
   golden journeys still failing after the preceding packages. Companion work
   remains out of scope.

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
