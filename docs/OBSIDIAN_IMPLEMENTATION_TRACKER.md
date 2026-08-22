# Obsidian free Android integration tracker

- Updated: 2026-08-22
- Branch: `release/0.207-obsidian-20260821`
- Baseline: Friday `0.206.4` (`caa01c23`)
- Target: Friday `0.207.1`
- Architecture: `outer_sol/OBSIDIAN_INTEGRATION_ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md`

## Release boundary and status

The implemented first-release slice is one owner-scoped Syncthing profile, one
Android device and one logical vault, with resumable private-Telegram onboarding,
native list/search/read/create/append/properties/daily-note operations, a durable
operation ledger, exact delivery facts and basic conflict diagnostics.

Automated implementation and the native immutable-release cutover are present,
but **physical-device acceptance has not been recorded**. Treat the Android
workflow as beta until the manual matrix below passes. The proposed
Syncthing-Fork acceptance floor, 2.1.0.0+, remains a candidate until then.

The current Telegram panel has copy-text, selectable text and a one-use HTTPS
fallback. It does not expose QR, and no happy-path step requires QR. The open
launcher is currently for `Friday Connection Test.md`; arbitrary-note navigation
is not part of this slice.

## Planning record

Initial engineering estimate from the architecture review:

- testable P0-P4 Android beta: 43-65 engineer-days, or roughly 9-13 weeks for
  one engineer / 6-8 calendar weeks for two engineers with review capacity;
- complete P0-P7 product contour: 95-140 engineer-days;
- the first 12-hour checkpoint was scoped to a testable beta, not physical
  Android certification or the P5-P9 follow-ups.

The implementation task list is the checklist below. Release work is isolated
in a dedicated worktree because a parallel engineer is repairing the 0.206.x
line; only committed, verified bug-fix lineage is merged before sealing the
Obsidian release.

## First-release checklist

- [x] P0: versioned contracts, onboarding/error states and distinct sync facts.
- [x] P0: fail-closed server Syncthing probe for `[2.1.3, 2.2.0)`.
- [x] P1: owner-hashed private profile/config/index/vault roots and bounded process manager.
- [x] P1: authenticated typed REST adapter for status, pending devices, config,
  scan, completion, file availability, connections and events.
- [x] P1: restart convergence, configuration-drift checks and diagnostics.
- [x] P2: durable, resumable and idempotent onboarding aggregate.
- [x] P2: private `/obsidian`, exact first-button `copy_text`, selectable full ID
  and fragment-only, one-use HTTPS fallback.
- [x] P2: pending-device auto-selection only when unique; explicit selection on ambiguity.
- [x] P2: folder offer/acceptance observation, `/obsidian_alias <exact name>` and
  delivery-before-open-confirmation round trip.
- [x] P3: no-follow contained atomic Markdown store and bounded frontmatter codec.
- [x] P3: list, lexical search, read, create, append, typed properties and daily notes.
- [x] P3: owner-scoped idempotent operation ledger and delivery postconditions.
- [x] P3: expected-revision conditional publication with atomic inode capture,
  Linux `renameat2(RENAME_EXCHANGE)` publication, durable adjacent journal,
  crash recovery and preserve-both handling for a racing peer write.
- [x] P4: separate local commit, server scan, live connection, Android receipt
  and user-confirmed open facts; delivery rechecks the exact revision on both
  sides of Syncthing observations, and offline delivery remains pending.
- [x] P4 minimum: preserve/detect conflict copies, exclude them from ordinary
  note results, report them in diagnostics and enable bounded staggered versioning.
- [x] Register the optional first-party Organ, capabilities, tools, router and reconciler.
- [x] Add focused automated coverage for configuration, storage, paths, routes,
  onboarding, operations, runtime, Syncthing adapter/process and Telegram transport.
- [x] Run and record the repository-wide isolated Python/static/UI release gate
  on the final functional state (Docker deliberately excluded from this release).
- [ ] Run and record physical Android/Syncthing-Fork acceptance.
- [x] Finish the immutable operator enablement with one recovery set for
  SQLite, Telegram inbox and exact `FRIDAY_OBSIDIAN_ROOT`.

## Physical-device acceptance still required

- [ ] Clean-install current Obsidian and each supported Syncthing-Fork version,
  starting with candidate 2.1.0.0+; record Android/OEM versions.
- [ ] Complete the path on one phone by copying Friday Device ID; use no QR,
  desktop, second display, Obsidian account or paid service.
- [ ] Exercise manual selectable-ID and one-use HTTPS fallbacks.
- [ ] Grant shared-storage access, accept `Friday` as Send & Receive at a chosen
  device-storage path, and open that exact folder as an Obsidian vault.
- [ ] Verify a non-default Unicode vault name through `/obsidian_alias`.
- [ ] Prove the order: Android receipt first, open button second, user open
  confirmation last, and only then onboarding `ready`.
- [ ] Write with Android offline/backgrounded, then prove later delivery after reconnect.
- [ ] Restart backend and prove profile/device/folder identity survives.
- [ ] Observe both direct and relay behavior where available and record diagnostics.
- [ ] Create an actual concurrent-edit conflict and prove both files survive and
  ordinary search excludes the conflict copy.
- [ ] Perform a stopped-process backup/restore drill with SQLite plus the exact
  `FRIDAY_OBSIDIAN_ROOT` generation.

## Known first-release limitations

- The topology is one owner, one Android device and one logical vault per
  isolated profile; shared or multi-device vaults are outside this slice.
- The supported note surface is Markdown list/lexical search/read/create/append,
  typed property updates and daily notes. There is no prepend/replace/move/delete,
  template/task/Base/graph/semantic/ingestion workflow or conflict-resolution UI.
- POSIX offers no general pathname CAS. On supported Linux this slice uses a
  bounded conditional exchange: capture and lease the current inode, stage the
  proposal, durably journal it beside the vault and publish with
  `renameat2(RENAME_EXCHANGE)`. It then verifies which generation won. A racing
  peer generation remains canonical, Friday preserves both sides as conflict
  copies and the operation fails closed as `conflict`; compare/merge/resolution
  remains manual.
- Vault read/traversal ceilings (4 MiB per note, 20,000 entries, 5,000 Markdown
  paths, 32 MiB aggregate Markdown, 1,000 list and 100 search results) are not a
  filesystem quota. Attachments or a peer can still exhaust disk; deployment
  needs an external filesystem/container quota and free-space monitoring.
- Conflict files are preserved and surfaced, but compare/keep/merge/dismiss is manual.
- `ready` records the completed onboarding proof; live availability is separately
  `android_connected`, `android_offline` or `unavailable`.
- The ordinary `jericho backup` command still covers SQLite only. Immutable
  release activation separately snapshots, verifies and crash-recoverably
  restores SQLite, Telegram inbox and the exact whole configured Obsidian root
  as one bounded recovery set. General disaster-recovery export remains the
  stopped-process encrypted snapshot procedure in `BACKUP_AND_RESTORE.md`.

## Follow-up phases, not first-release gates

- [ ] P5: stable note IDs/bindings, links/backlinks, unresolved/orphan/dead-end
  graph and identity-preserving moves.
- [ ] P6: semantic passage index, multi-lane retrieval, Active Frame/candidate
  sets, Playbooks and operational-memory integration.
- [ ] P7: durable tasks, typed Bases, managed regions, Friday bindings and explicit
  Obsidian-to-Friday Inbox ingestion.
- [ ] P8: optional Android companion for current note, selection, cursor and
  native commands; never the sync backbone.
- [ ] P9: pooled Syncthing, supported Android helper/intents, alternate transports,
  desktop adapter, optional MCP facade and packaged companion release.

## Verification log

- 2026-08-21: architecture v0.4 read in full; source hash
  `a765326453b62e1eed4bb9f2df53ca46d839ee6bde892f92a82506022480a0f8`.
- 2026-08-21: first-release code/config/routes and focused test contracts audited
  against this tracker. This is source inspection, not physical-device evidence.
- 2026-08-21: isolated final functional gate: 14,815 non-UI tests passed
  (2 explicit live/installer opt-in skips), 31 Playwright UI tests passed;
  Ruff, format, mypy, compileall, Bandit HIGH and JavaScript syntax passed.
  The pinned Syncthing 2.1.3 live REST/config smoke passed separately. The
  canonical toolchain identity preflight was not used because the host carries
  pre-existing Node/unrar identity drift; Docker certification was deliberately
  excluded by operator direction.
- 2026-08-22: the two latest live Office-review failures were replayed against
  their exact authenticated message/raw lineage. Legacy ODT/XLSX documents can
  now receive a transient current-parser index for whole/exact review without
  mutating stored provenance; empty tabular XLSX profiles fall back safely.
  The one-repair review lane is rechecked by code rather than a second
  same-model judge, and local entity/date/quantity/bound/percent scopes reject
  cross-record drift while preserving grounded Russian paraphrases.
- 2026-08-22: sealed release gate on the final functional snapshot: 15,180
  non-UI tests passed with 2 declared opt-in live/installer skips, 31/31
  Playwright UI tests passed, and Ruff, format, mypy, compileall, Bandit HIGH
  plus JavaScript syntax all passed. The pinned Syncthing 2.1.3 live smoke
  passed separately. Known host-only Node/unrar identity drift remains outside
  the source checks; Docker certification was deliberately excluded by
  operator direction.
