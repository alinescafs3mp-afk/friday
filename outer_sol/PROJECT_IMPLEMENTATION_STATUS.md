# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-25
- Branch: `main`
- Secondary acceptance base: live `0.207.27` admits exactly one accepted
  profile in bounded `assist`; the provisional registry is empty and the
  primary remains the only publication, tool and effect authority.
- Deployed implementation head: `b7378686a70465fbdc9a7b27aef1e0bbae5edd52`
- Live: Friday `0.207.27` / `b7378686a70465fbdc9a7b27aef1e0bbae5edd52`;
  tree `7ce723b56aafcf80cdde21c86717d3973e79884c605d175df87c7cf4b0c0f076`;
  wheel `60b05e50a08107dc185e8a6cbd52f3bf2e18d5dd53a08b805a1a9f0390b128ae`
- Immediate predecessor and schema-capable fallback: Friday `0.207.26` /
  `4f33a6922389ccb70eb92b2fd8d0e4f2aced20a5`, tree
  `36ebf876af7cc41fcd47c8e23d546b505e5d6952fda52f4ea1f1eb7cff531d7c`.
- Database schema: 42
- Production state: immutable activation `clear`; backend and Telegram bridge
  active; trusted-CA HTTPS health `200`; SQLite integrity and FK checks clean;
  exact accepted profile is healthy in bounded `assist` and is probed on
  demand; laptop absence preserves the primary-only path. The primary V12
  canary is installed for `archive_read` and `file_read` with a live exact
  attestation. The exact,
  body-free schema-41 DocumentCatalog now has its bounded durable worker and
  archive consumer in production. Obsidian create/append now publish an atomic
  body-free effect outcome and reconcile uncertainty by observation without
  replaying a vault mutation. Catalog coverage remains honestly incomplete
  while the backlog converges.
- Delivery constraints: no Docker for primary Friday release certification;
  companion plugin untouched; small commits and immutable wheel-only production
  releases. The optional laptop inference node is a separate Docker contour.

## Active objective

Complete the next bounded ICP/V12 golden journey while preserving the deployed
optional GPT-OSS-20B/SGLang assist and exact primary-only behavior whenever the
laptop is absent. Public shadow, private shadow, bounded assist, exact physical
loss and automatic recovery are complete. The schema-41 DocumentCatalog,
archive candidate runtime, receipt-backed Obsidian effect vertical and
selected-evidence V12 explanation are live. The next autonomous package is the
next incomplete ICP/V12 golden journey and its release evidence.

The measured and accepted finalist is exact profile
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
accepted profile manifest hashes to
`93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3` and
its accepted physical-failure evidence hashes to
`9dc72f80caed3320bd154cf1219a8bd6b1339142b690b00dd1cbe1fb05964006`.
Live `0.207.27` admits exactly that profile, with no provisional entries, in
bounded `assist`. It grants no tools, effects, publication or knowledge-write
authority; failure preserves the exact primary-only path.

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

1. Release `0.207.28` with the bounded GPT-OSS document-map lane in private,
   discarded `shadow`; primary MAP/REDUCE and final synthesis remain
   authoritative and laptop absence preserves the existing primary path.
2. Bind fresh, identity- and policy-specific shadow evidence before any
   separately released `document_map=assist` promotion; counters alone cannot
   authorize promotion.
3. Complete the durable `CompareConversationWithDocument` journey: preserve
   exact selected message evidence, wait durably for the document, survive
   restart, recheck both authorities/revisions and publish one verified answer.
4. Release that journey reader-first, then activate its writer/runtime in a
   separate safe package with schema-capable fallback.
5. Extend machine-reconcilable release evidence without promoting
   component-only gates, then start the next V12 refinement package.
6. Continue with the next ICP/V12 refinement package after the complete golden
   journey. The accepted laptop runtime and gateway manifest remain unchanged.

## Completed and deployed packages

### Systemic document-contour reliability (`0.207.27`)

- Read-only document review no longer falls through to the supported-deed
  completion guard, shifted ordinal spreadsheet requests preserve their row
  selection, and secondary Inbox extraction uses a strict JSON schema.
- LibreOffice conversion always receives an isolated trusted `/tmp` sandbox,
  independent of an ambient test or service `TMPDIR`.
- The exact source passed 18,903 non-UI and 31 UI tests with zero skips.
  Immutable activation completed `clear`; both services, schema-42 integrity
  and foreign-key checks are clean.

### Bounded optional GPT-OSS assist (`0.207.26`)

- The exact accepted profile is eligible only for bounded advisory extraction;
  the primary remains final and owns every answer, tool, effect and write.
- Immutable `secondary_shadow_to_assist` activation completed `clear` with
  terminal receipt
  `1b07dac7750d744d0ab8c9998418e58985751966a71b85e65e771ad460d4bf86`.
  The fresh assist product receipt hashes to
  `7b4fa2b40f70047020e29e00a74498ee5bef13a2153a0a5e51ce9f9647f79a80`.
- A full authenticated request was interrupted by physical laptop power loss;
  Friday's primary PID and release remained unchanged, the endpoint failed
  closed, and the exact candidate recovered automatically. The causal and final
  receipts hash to
  `194096eca5718c5071af1fea2414f3539a4ddeefa8ccf7e39ca302fb1f44974b`
  and `4b037955f83335bc9065e652a6cdb2014607a06ce71e9c9afc5bb98b6de45c49`.

### Private discarded GPT-OSS shadow (`0.207.24`)

- Private typed extraction runs only in discarded `shadow/extract`; response,
  tools, effects and publication remain primary-owned.
- The exact source passed 18,666 non-UI and 31 UI tests; two wheels reproduced
  byte-for-byte. Immutable activation completed `clear`, health is `ok`, both
  services are active and schema-41 integrity/FK checks are clean.

### Accepted public GPT-OSS shadow (`0.207.23`)

- The exact accepted profile is live in public discarded `shadow/extract`; the
  provisional registry is empty and private input, assist, tools, effects and
  publication remain closed.
- The exact source passed 18,655 non-UI and 31 UI tests; two wheels reproduced
  byte-for-byte. Immutable activation completed `clear`, health is `ok`, both
  services are active and schema-41 integrity/FK checks are clean.

### Natural questions about selected archive evidence (`0.207.22`)

- Bounded RU/EN content questions reuse one exact durable archive selection
  through the existing attested V12 explanation. Compound effects, external
  sources and current attachments remain on their explicit routes.

### Attested selected-archive explanation (`0.207.21`)

- A closed explain follow-up to one durable selected document or message uses
  one exact V12 lease for synthesis plus an independent verifier. The model sees
  only bounded exact passage projections and every accepted fact retains nested
  passage citations.
- Authority, source revision, evidence identity and lease are rechecked before
  one atomic message/receipt/Work-Item CAS. Model, verifier, lease and source
  failures fall back to exact structural replay without ordinary-model retry.
- The release-blocking battery now pins Obsidian mutation, archive isolation and
  continuation, QNAP/web isolation, repeated Office reads, dialogue and V12
  ordinal journeys. The exact gate passed 18,454 non-UI and 31 UI tests; two
  wheels reproduced byte-for-byte. Immutable activation completed `clear`,
  health is `ok`, both services and V12 routes are active, and schema 41
  integrity/FK checks are clean.

### Reader-first explanation compatibility (`0.207.20`)

- Outcome and Work Item readers learned the new explanation receipt before the
  writer was enabled, making `0.207.20` the schema-capable rollback target for
  `0.207.21` without a schema change.

### Receipt-backed Obsidian effect reconciliation (`0.207.19`)

- A closed `EffectOutcomeV1` stores only domain-separated digests, structural
  authorization/reconciliation facts and independent server, sync, re-ingest
  and physical observations. Raw note bodies, paths and arguments never enter
  the accepted receipt or public projection.
- Create/append publication stores the accepted outcome atomically with the
  assistant message. Accepted ledger results are immutable; late authorization
  loss suppresses public facts without erasing historical proof.
- Prepared/uncertain work reconciles only from an exact private sidecar or the
  bounded legacy create postcondition. Reconciliation observes and never
  replays a vault mutation; later user edits cannot be overwritten.
- The exact source passed 18,436 non-UI and 31 UI tests with pinned Syncthing
  2.1.3 and zero skips. Two clean wheels reproduced byte-for-byte. Immutable
  activation completed `clear`; health is `ok`, both services resolve to the
  sealed candidate, schema 41 integrity/FK checks are clean, and the canonical
  gate now removes its own multi-gigabyte pytest scratch trees.

### Bounded DocumentCatalog convergence and archive consumption (`0.207.18`)

- The durable worker performs bounded, checkpointed body-free catalog
  backfill/reconciliation outside startup and request paths. The archive
  consumer uses only current authorized navigation metadata; stale, missing or
  incomplete rows remain explicitly non-current rather than proving absence.
- The `0.207.18` hotfix preserves the fair first pass, then reclaims only
  measured unused tenant/phase reservations for successful phases that still
  have work. The global tick bound and failure reservations remain intact.
- The first two production ticks backfilled 46 and 38 rows with zero phase
  failures. Coverage is still converging and is not claimed complete.
- The exact source passed 18,315 non-UI and 31 UI tests with zero skips. Two
  clean wheels reproduced byte-for-byte; immutable activation completed
  `clear`, both services are active, health is `ok`, and schema 41 integrity
  and foreign-key checks are clean.

### Body-free DocumentCatalog foundation (`0.207.16`)

- Schema 41 installs an exact rebuildable sidecar bound to Raw Object revision
  and content hash. It stores only hashes, a navigation-only explicit heading,
  status/reason and timestamp; no body, excerpt, arbitrary metadata or model
  prose is persisted.
- Trigger and storage contracts fail closed on stale, altered, cross-tenant or
  invalid source bindings. Bounded indexed keyset rebuild/backfill/reconcile
  APIs preserve opaque Raw IDs and admit bodies one at a time under item/byte
  budgets.
- Production migration completed `clear`: all 1,985 live file Raw rows have an
  exact `incomplete` catalog row, schema/FTS markers are 41, integrity/FK checks
  are clean, and the catalog fingerprint is
  `c14efeb3addb684d1d65fa03358267f24bc0abb7a411e07524e04ecf04b14b90`.
- The exact source passed 18,280 non-UI and 31 UI tests with pinned Syncthing
  2.1.3 and zero skips. Two clean wheels reproduced byte-for-byte; a distinct
  code-identical schema-41 fallback was sealed before activation.

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
- a second exact cleanup removed 14 additional clean, unlocked, detached
  ancestor worktrees only through `git worktree remove`, releasing about
  784 MiB; their tracked history remains in Git and only regenerable caches and
  build output were discarded;
- the three superseded WIP refs were preserved with complete history in the
  verified forensic bundle
  `/home/jericho/.jericho/forensics/document-file-contour-wip-20260822/document-file-contour-wip-20260822.bundle`,
  SHA-256
  `bc1b201a925880cd7c3e75a23524fa563274b9ea55973c6406dcb934b1e54eef`,
  then deleted by exact old-OID guards. No object pruning or garbage collection
  was run; the 824 unique evidence blobs remain recoverable from the bundle.

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
| `obsidian_write_sync` | Obsidian write and synchronization | `UNVERIFIED` | `AVAILABLE`<br>[friday/organs/obsidian/contracts.py](../friday/organs/obsidian/contracts.py)<br>[friday/orchestration/effect_outcome.py](../friday/orchestration/effect_outcome.py)<br>[tests/test_effect_outcome.py::test_effect_outcome_is_immutable_canonical_closed_and_round_trips](../tests/test_effect_outcome.py)<br>[tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions](../tests/test_obsidian_structured_acceptance_core.py) | `AVAILABLE`<br>[friday/organs/obsidian/runtime.py](../friday/organs/obsidian/runtime.py)<br>[tests/test_agent_obsidian_acceptance_message_matrix.py::test_every_exact_tier_a_b_message_routes_through_full_chat_once](../tests/test_agent_obsidian_acceptance_message_matrix.py)<br>[tests/test_agent_obsidian_production_composition.py::test_note_create_append_and_daily_exact_messages_mutate_the_real_vault](../tests/test_agent_obsidian_production_composition.py) | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract](../tests/test_obsidian_syncthing_live.py) | `MISSING` | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text](../tests/test_obsidian_runtime.py)<br>[tests/test_obsidian_operations.py::test_unproved_append_stays_uncertain_and_never_mutates_the_vault](../tests/test_obsidian_operations.py) | `MISSING` | `MISSING` | `physical_android_round_trip_missing`<br>`real_conflict_evidence_missing` |
| `durable_scheduled_work` | Durable scheduled work | `UNVERIFIED` | `AVAILABLE`<br>[friday/reminder_schedule.py](../friday/reminder_schedule.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `AVAILABLE`<br>[friday/storage/_missions.py](../friday/storage/_missions.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_mission_budgets_and_recovery.py::test_spent_budget_survives_a_restart](../tests/test_mission_budgets_and_recovery.py)<br>[tests/test_mission_budgets_and_recovery.py::test_an_interrupted_side_effect_is_never_replayed_blindly](../tests/test_mission_budgets_and_recovery.py) | `MISSING` | `MISSING` | `current_code_journey_audit_missing`<br>`at_most_once_delivery_recovery_missing` |
| `honest_degradation` | Honest degradation | `DEGRADED` | `AVAILABLE`<br>[friday/orchestration/capability_outcome.py](../friday/orchestration/capability_outcome.py)<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result](../tests/test_search_provider_refusal_is_not_emptiness.py) | `AVAILABLE`<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses](../tests/test_search_provider_refusal_is_not_emptiness.py)<br>[tests/test_message_window_runtime_integration.py::test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried](../tests/test_message_window_runtime_integration.py) | `MISSING` | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_full_package_a_oracle_accepts_natural_honest_refusals](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution](../tests/test_message_window_work_item_runtime.py) | `MISSING` | `MISSING` | `product_multi_lane_coverage_missing`<br>`candidate_bound_fault_continuation_evidence_missing` |

## Current cumulative gate

- The last full canonical base gate, exact `0.207.24`, passed 18,666 non-UI
  tests plus 31 UI tests, including pinned Syncthing 2.1.3, with zero skips and
  byte-identical wheels. The bounded `0.207.26` assist hotfix then passed its
  focused release/secondary gate and reproduced its wheel byte-for-byte.
- Friday `0.207.26` bounded assist and the primary V12 canary are live at the
  exact source/tree/wheel identity above, schema 41, with `0.207.24` as both
  predecessor and schema-capable fallback. Immutable activation is `clear`;
  backend and bridge are active, health is `ok`, and SQLite integrity/FK checks
  are clean.
- At-logon recovery, a causal physical mid-request power cut and exact
  candidate recovery passed without restarting Friday. The optional node
  remains demand-probed and removable; its absence does not widen retries or
  change primary publication semantics.

## Next order

1. Complete and release the durable `CompareConversationWithDocument` journey
   through reader-first schema capacity and a separate runtime writer package.
2. Extend immutable release evidence into machine-reconcilable journey
   manifests without turning component tests into readiness claims.
3. Begin the next V12 refinement selected by the remaining golden-journey gap.
4. Run actual Android/Syncthing, backup/clean-restore and bounded recovery/fault
   certification; simulations remain labelled separately.
5. Evaluate generic WorkGraph, broader effects or connectors only from the
   golden journeys still failing after the preceding packages. Companion work
   remains out of scope.

## WIP source retention

`DOCUMENT_FILE_CONTOUR_WIP_AUDIT_2026-08-22.md` was recovered at
`/home/jericho/DOCUMENT_FILE_CONTOUR_WIP_AUDIT_2026-08-22.md`, outside the
repository location named by the operator. Its recommendations are reconciled
above. The source remains retained; the three former refs are now represented
by the verified bundle and privacy-safe manifest rather than live repository
refs.

## Update rule

After each released package, update at least: source/live/previous identities,
production health, completed package, current gate and next ordered item. Do not
mark device-dependent or external-service observations complete from local
tests.
