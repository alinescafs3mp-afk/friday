# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-23
- Branch: `main`
- Deployed implementation head: `c91260d6f8f74e3276851ebfd42916a2af4396db`
- Live: Friday `0.207.4` / `c91260d6f8f74e3276851ebfd42916a2af4396db`;
  tree `fc48dbf365865ac2b5e0230c9320c0c2b2fd76f7cf0856d4e822c29900c2f519`;
  wheel `bec63678e23965754529b93673a32796d99a2d371b1c2693b5990a855526886d`
- Schema-capable fallback: `cb1b3f71166afe0a1a6fd277dfe0440ef292ed0b`;
  tree `0363a0646158001b574e9bca468e2fc480fbd9e30f00b1d1f4ac08f32d6ce15f`
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

1. Define the canonical golden-journey/evidence registry and make readiness
   claims mechanically traceable to contracts, tests and release evidence.
2. Audit existing retrieval identity and lifecycle boundaries before adding the
   smallest stable `SourceRef`/passage/coverage foundation.

## Provisional golden-journey view

No journey is currently declared `READY` merely from component tests.

| Journey | State | Missing decisive evidence |
|---|---|---|
| Conversation recall | `DEGRADED` | semantic recall and cross-lane coverage beyond the deployed exact-window journey |
| Document recall and answer | `DEGRADED` | stable source/passage identity; cross-lane coverage; durable continuation |
| Obsidian write and synchronization | `UNVERIFIED` | common effect envelope; reconciliation; actual Android round trip/conflict evidence |
| Durable scheduled work | `UNVERIFIED` | current-code journey audit and at-most-once/recovery evidence |
| Honest degradation | `DEGRADED` | product-level multi-lane coverage and fault evidence |

## Current cumulative gate

- 16,175 non-UI Python tests passed on 16 workers with no failures or skips.
- The pinned Syncthing `v2.1.3` managed-REST smoke passed; this is not evidence
  of an Android round trip.
- Schema-38 fixture, lifecycle/privacy, store adversarial, runtime continuation,
  migration-chain and existing named-inventory compatibility checks are green.
- Ruff, release-surface format (871 files), mypy (210 source files), compileall,
  Bandit HIGH, JavaScript syntax and toolchain preflight passed.
- The release wheel was built twice from the clean commit archive and matched
  byte-for-byte. Immutable activation completed `clear`; schema-38 fallback,
  database/inbox backups, trusted-CA health, exact process roots, SQLite
  quick-check, foreign keys and Work Item DDL were verified.

## Next order

1. Add one concise canonical golden-journey/evidence registry with strict
   `READY`, `DEGRADED`, `UNVERIFIED`, `BLOCKED` and `OUT_OF_SCOPE` states.
2. Design and release the retrieval-identity foundation: `SourceRef`, rebuildable
   `CatalogItem`, `PassageRef`, typed `TemporalFact` and `SearchCoverage`, with
   authoritative revalidation and no date-role substitution.
3. Release one read-only federated `archive_search` facade with deterministic
   continuation, neighboring message context and explicit per-lane coverage.
4. Extend the proven recall Work Item across document and message evidence only
   through stable source/passage references and fresh authorization/revision
   checks.
5. Add one uncertainty-aware common effect envelope and prove one idempotent,
   receipt-backed Obsidian mutation/reconciliation vertical.
6. Extend existing immutable-release evidence into a machine-reconcilable
   source/wheel/schema/activation/fallback manifest and clean-artifact proof.
7. Run actual Android/Syncthing, backup/clean-restore and bounded recovery/fault
   certification; simulations remain labelled separately.
8. Evaluate generic WorkGraph, broader effects, connectors or companion work
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
