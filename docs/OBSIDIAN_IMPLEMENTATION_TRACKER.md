# Obsidian free Android integration tracker

- Updated: 2026-08-25
- Branch: `main`
- Source/deployed baseline: Friday `0.207.21` at `27b9fc5`
- Target: Friday `0.207.21`, schema 41
- Architecture: `outer_sol/OBSIDIAN_INTEGRATION_ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md`
- Acceptance: `outer_sol/OBSIDIAN_INTEGRATION_ACCEPTANCE_BATTERY.md`

## Release boundary and status

The release implements the Tier A and Tier B core battery for one owner-scoped
Syncthing profile, one Android device and one logical vault. Telegram remains
the user interface; the companion plugin is neither required nor included.

The complete server-side acceptance surface is implemented and covered by the
exact Russian requests from the battery. Automated gates are green. A physical
Android/Syncthing-Fork run is still required for observations that cannot be
produced by the backend itself: one-phone onboarding, an edit made on the phone,
offline reconnect delivery and a real concurrent-edit conflict.

## Implemented core

- [x] Resumable one-phone onboarding with copy-text Device ID, selectable text,
  one-use HTTPS fallback, automatic pending-device discovery and one logical
  Send & Receive vault.
- [x] Configurable clean-vault name through `FRIDAY_OBSIDIAN_VAULT_NAME`; the
  acceptance environment can use `Friday-Test` without renaming an existing
  vault.
- [x] Owner/vault containment, atomic Markdown writes, expected-revision checks,
  durable adjacent journals and operation-ledger idempotency.
- [x] Markerless user-visible Markdown. Operation identity and arguments live in
  a private external SQLite receipt store; legacy HTML markers are migrated out.
- [x] Distinct `server_committed`, `server_scan_complete`,
  `android_delivered` and `obsidian_open_confirmed` facts. Offline delivery is
  pending, never reported as a permanent write failure or a false phone receipt.
- [x] Exact-path create, append, daily-note append, typed properties and tags,
  section replacement, move and delete.
- [x] Exact-path prepend to the Markdown body while preserving frontmatter, and
  complete note-content replacement behind mandatory `expected_revision` CAS.
- [x] Prepend/replace operation-ID idempotency, frozen targets and receipt-gap
  crash reconciliation; retries cannot duplicate a prepend or overwrite a
  revision that changed concurrently.
- [x] Markdown-aware section handling that ignores headings inside YAML,
  fenced code and HTML comments.
- [x] Dated tasks and incomplete-task lookup with source path and concrete local
  date/time.
- [x] Incremental indexing of Friday- and Android-originated notes, composite
  content/date search, excerpts, provenance and honest partial-coverage reports.
- [x] Stable note identities, wikilinks/backlinks, identity-preserving moves and
  conservative link rewriting with unresolved references reported separately.
- [x] Persisted ordered candidate sets and Active Frames for “the second one” /
  “there” continuation without re-running an ambiguous search.
- [x] Template rendering, structured conversation summaries with stable Work
  Item IDs, and continuation that appends links without replacing the body.
- [x] Body-free recursive template listing from the configured template folder,
  with deterministic caps, revision/modified metadata and symlink-safe
  containment; missing template folders return an empty list.
- [x] `.base` generation plus a server-side `BaseSpec` evaluator over current
  indexed revisions; property changes affect the next query.
- [x] Revision-pinned deletion/tombstone lifecycle, stale-passage invalidation,
  backlink invalidation and Active Frame invalidation.
- [x] Preserve-both conflict discovery in the normal `/obsidian` panel,
  dual-revision merge preview and explicit acceptance. Conflict artifacts are
  retained and never removed automatically.
- [x] Crash recovery after filesystem commit or projection failure for create,
  append, move, delete and conflict resolution. Resume reuses the original
  operation/Work Item identity and reconciles the postcondition before retrying.
- [x] Create/append publish one body-free accepted effect outcome atomically
  with the assistant message. Prepared/uncertain operations reconcile only by
  exact private proof or bounded legacy observation and never replay a vault
  mutation; accepted results remain immutable after later user edits.
- [x] HTTPS Telegram `Open in Obsidian` action for the exact arbitrary note path;
  Friday does not claim the app opened without an explicit confirmation.
- [x] Account deletion covers schema-37 Obsidian projections and owner-scoped
  operational state.

## Acceptance coverage

- [x] OBS-NOTE-01/02: create and append once.
- [x] OBS-DAILY-01: local-day daily append with section reuse.
- [x] OBS-TASK-01 and OBS-META-01: tasks, properties and tags.
- [x] OBS-SEARCH-01/02 and OBS-SYNC-01: paraphrase/date search and incremental
  Android-origin ingestion.
- [x] OBS-CONT-01: stable second-result selection and active-note continuation.
- [x] OBS-LINK-01 and OBS-MOVE-01: backlinks and identity-preserving move.
- [x] OBS-TEMPLATE-01, OBS-WORK-01 and OBS-BASE-01.
- [x] OBS-OFFLINE-01: simulated offline→reconnect delivery of the same revision.
- [x] OBS-CONFLICT-01: deterministic preserve-both record, preview, acceptance
  and crash-tail recovery.
- [x] OBS-RECOVERY-01: runtime/service reconstruction with the same operation ID
  and exactly one durable line.
- [x] OBS-DELETE-01: synchronized tombstone and closed search lifecycle.
- [x] Post-battery prepend/replace extension: closed conversational grammar,
  tool schemas, path containment, frontmatter-at-EOF preservation, revision CAS,
  exactly-once replay and server registration.
- [x] Post-battery template-list extension: exact Russian conversational route,
  read-only tool/kernel registration, bounded deterministic subtree scan and a
  closed body-free result projection.
- [ ] OBS-ONB-01 and the physical portions of SYNC/OFFLINE/CONFLICT: record on a
  real Android phone before claiming physical-device certification.

## Verification log

- 2026-08-22: exact-message acceptance matrix covers all Tier A/B commands and
  keeps mutation payloads/path selection code-owned.
- 2026-08-22: focused Obsidian gate: 580 passed, 1 declared live Syncthing skip.
- 2026-08-22: full isolated Python gate on 24 workers: 15,591 passed, 2 declared
  opt-in skips. Ruff, format, mypy (195 files), compileall and `git diff --check`
  passed. Docker was deliberately excluded by operator direction.
- 2026-08-22: final acceptance hotfix gate: 15,672 non-UI and 31 UI tests
  passed; Ruff, format, mypy (196 files), compileall, Bandit HIGH and browser
  preflight passed. Docker remained deliberately excluded.
- 2026-08-22: signed production `/api/chat` smoke passed create, read, append,
  search, properties, move and delete. Temporary notes and legacy markers both
  returned to zero; mutations correctly remained `scan_pending` until Android
  synchronization can prove delivery.
- 2026-08-22: calendar-boundary proof checks 2026-08-21 22:30 UTC as
  2026-08-22 in `Europe/Berlin`, including the NOTE-01 content and receipt.
- 2026-08-22: offline and recovery tests prove pending→delivered transition,
  runtime reconstruction, original operation reuse and absence of duplicate
  bytes, paths or ledger rows.
- 2026-08-23: prepend/replace landed in `253963d`. Blocker-focused verification
  passed 199 tests; the expanded Obsidian/agent-route set passed 256; isolated
  server tool registration passed. Ruff check/format, scoped mypy and
  `git diff --check` passed.
- 2026-08-23: the broader Obsidian invocation produced 516 passes and one
  expected live Syncthing skip. It is not recorded as a full-suite pass because
  12 collection errors came from the workstation's deliberately ambiguous pair
  of local databases, not from an exercised Obsidian assertion.
- 2026-08-23: `b50e63b` added atomic accepted-outcome receipts for the V12 read
  routes; its focused gate passed 190 tests. `6a25cda` added safe template
  listing; two independent focused runs passed 227 tests, Ruff/format/scoped
  mypy and review were green.
- 2026-08-23: cumulative non-UI Python gate on source `321f8fa` passed all
  15,874 selected tests, including the pinned Syncthing `v2.1.3` smoke, with
  zero skips. Toolchain preflight, Ruff, format (856 files), mypy (204 source
  files), compileall, Bandit HIGH and both JavaScript syntax checks passed.
  Docker and the separate unchanged browser/UI phase were not run.
- 2026-08-25: `0.207.19` deployed the receipt-backed effect/reconciliation
  vertical. The exact gate passed 18,436 non-UI and 31 UI tests with pinned
  Syncthing 2.1.3 and zero skips; two wheels reproduced byte-for-byte.
  Activation is `clear`, backend/bridge are active, health is `ok`, and schema
  41 integrity and foreign-key checks are clean. Physical Android evidence is
  still deliberately unclaimed.
- 2026-08-25: `0.207.21` made the exact Obsidian/archive/web/file/dialogue
  product battery release-blocking. The canonical gate passed 18,454 non-UI
  and 31 UI tests; two wheels reproduced byte-for-byte and immutable activation
  completed `clear`. No companion-plugin or physical-device claim was added.

## Physical-device acceptance still required

- [ ] Use a clean dedicated `Friday-Test` vault and test chat.
- [ ] Complete onboarding on one phone with no QR, desktop, Obsidian account or
  paid Sync; record Device ID/folder identity and the round-trip proof.
- [ ] Create/edit a note in mobile Obsidian and verify incremental server ingest
  and Android/user-owned provenance.
- [ ] Stop Syncthing-Fork, write through Friday, reconnect and record delivery of
  the same operation/revision without a duplicate.
- [ ] Produce a real concurrent edit, verify both files survive, inspect the
  merge preview and explicitly accept it.
- [ ] Record the distinction between Android delivery, the offered open action
  and explicit open confirmation.

## Known limits

- The released topology is one owner, one Android device and one logical vault
  per isolated profile. Shared and multi-device vaults remain out of scope.
- The server evaluator supports Friday's documented `.base` subset; it does not
  claim to execute a running native Obsidian engine.
- Search is a bounded server-side multi-lane approximation over revision-pinned
  note passages and metadata. It reports incomplete coverage instead of making
  a false absence claim.
- Conflict merge is intentionally explicit and preserve-both. Friday never
  deletes a Syncthing conflict artifact automatically.
- Vault read/traversal ceilings are safety bounds, not filesystem quotas;
  deployment still needs ordinary free-space monitoring and backups of SQLite,
  Telegram inbox and the exact Obsidian root.
- The optional companion-plugin Tier C remains unimplemented and is not part of
  this release gate.

## Deferred work

- [ ] Physical Android/Syncthing-Fork evidence matrix and backup/restore drill.
- [ ] Multi-device/shared-vault topology and alternate transports.
- [ ] Optional companion plugin for foreground selection/cursor actions; it may
  never become a dependency of the Syncthing-backed core.
- [x] Interaction Control Plane work resumed after this acceptance release;
  P0A/P0B structural tracing and the aggregate admin baseline are tracked in
  `outer_sol/INTERACTION_CONTROL_PLANE_IMPLEMENTATION_STATUS.md`.
