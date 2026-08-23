# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-23
- Branch: `main`
- Repository head: `0f47870` (dormant schema-38 Work Item foundation);
  convergence brief: `ed50d38`; implementation live code remains `4c02ab8`
- Live: Friday `0.207.4` / `4c02ab8e3bbfac4f56d9e838dd016afb7c55711e`
- Previous/fallback: `272b64c4dcd2aa80ea368a70efe6cd6083d70095`
- Database schema: 37
- Production state: immutable activation `clear`; backend and Telegram bridge
  active; trusted-CA HTTPS health `200`; SQLite integrity and FK checks clean
- Delivery constraints: no Docker; companion plugin untouched; small commits and
  immutable wheel-only production releases

## Active objective

Finish the current narrow P2 `RecallConversation` Work Item and temporal
follow-up canary as Friday's first durable golden journey. Then measure progress
by complete, recoverable user journeys rather than by isolated adapters: define
the journey/evidence registry, establish stable retrieval and passage identity
with honest coverage, expose one read-only archive facade, and only then extend
durable recall to documents. Keep `main` and the live release healthy after
every package.

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

1. Publish the implemented schema-38 foundation (`0f47870`) as a dormant
   immutable release for one narrowly scoped durable `RecallConversation` Work
   Item and bounded Active Frame.
2. Establish that schema-38 release as the rollback-safe fallback for the later
   behavior candidate.
3. Narrow behavior canary over the typed exact message-window lane: a full
   request creates the current Work Item; an immediate closed temporal follow-up
   such as `А вчера?` updates only its window while retaining the authorized
   conversation and role.

## Provisional golden-journey view

No journey is currently declared `READY` merely from component tests.

| Journey | State | Missing decisive evidence |
|---|---|---|
| Conversation recall | `DEGRADED` | durable follow-up release; semantic recall and coverage; restart/rollback journey proof |
| Document recall and answer | `DEGRADED` | stable source/passage identity; cross-lane coverage; durable continuation |
| Obsidian write and synchronization | `UNVERIFIED` | common effect envelope; reconciliation; actual Android round trip/conflict evidence |
| Durable scheduled work | `UNVERIFIED` | current-code journey audit and at-most-once/recovery evidence |
| Honest degradation | `DEGRADED` | product-level multi-lane coverage and fault evidence |

## Current cumulative gate

- 16,022 selected non-UI Python tests passed on 20 workers.
- The current P2 focused matrix adds 983 passed tests and one declared real-
  backup skip; schema-38 fixture, lifecycle/privacy, store adversarial, runtime
  continuation and migration-chain checks are green.
- One declared opt-in migration drill over copies of real operator backups was
  skipped; production backups were not used as a test playground.
- Ruff, release-surface format (871 files), mypy (210 source files), compileall,
  Bandit HIGH, JavaScript syntax and toolchain preflight passed.
- Two initial failures were stale synthetic web-kernel fixtures. `e4d4a46`
  updated them to emit the same exact query/tool identity attestation as the
  production kernel and added negative tests for missing/substituted queries;
  no production check was weakened.

## Next order

1. Complete and release the schema-38 foundation, then the exact message-window
   continuation canary with restart, expiry, cancellation, ownership,
   revision-CAS, receipt, atomic rollback and immutable fallback evidence.
2. Add one concise canonical golden-journey/evidence registry with strict
   `READY`, `DEGRADED`, `UNVERIFIED`, `BLOCKED` and `OUT_OF_SCOPE` states.
3. Design and release the retrieval-identity foundation: `SourceRef`, rebuildable
   `CatalogItem`, `PassageRef`, typed `TemporalFact` and `SearchCoverage`, with
   authoritative revalidation and no date-role substitution.
4. Release one read-only federated `archive_search` facade with deterministic
   continuation, neighboring message context and explicit per-lane coverage.
5. Extend the proven recall Work Item across document and message evidence only
   through stable source/passage references and fresh authorization/revision
   checks.
6. Add one uncertainty-aware common effect envelope and prove one idempotent,
   receipt-backed Obsidian mutation/reconciliation vertical.
7. Extend existing immutable-release evidence into a machine-reconcilable
   source/wheel/schema/activation/fallback manifest and clean-artifact proof.
8. Run actual Android/Syncthing, backup/clean-restore and bounded recovery/fault
   certification; simulations remain labelled separately.
9. Evaluate generic WorkGraph, broader effects, connectors or companion work
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
