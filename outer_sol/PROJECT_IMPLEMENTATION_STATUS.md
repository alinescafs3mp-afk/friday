# Friday project implementation status

This is the canonical short status register for the active Friday work. Detailed
design and acceptance evidence remains in the linked documents; this file owns
the current production identity, completed packages, active work and next order.

- Updated: 2026-08-23
- Branch: `main`
- Source: `e4d4a46` (`main`; implementation live code remains `4c02ab8`)
- Live: Friday `0.207.4` / `4c02ab8e3bbfac4f56d9e838dd016afb7c55711e`
- Previous/fallback: `272b64c4dcd2aa80ea368a70efe6cd6083d70095`
- Database schema: 37
- Production state: immutable activation `clear`; backend and Telegram bridge
  active; trusted-CA HTTPS health `200`; SQLite integrity and FK checks clean
- Delivery constraints: no Docker; companion plugin untouched; small commits and
  immutable wheel-only production releases

## Active objective

Finish the highest-value read-only CapabilityOutcome/completion-gate/accepted-
receipt adapters, verify the cumulative non-Docker baseline, then begin the
smallest useful P2 durable Work Item and Active Frame slice. Keep `main` and the
live release healthy after every package.

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

1. P2 schema-38 foundation for one generic durable `RecallConversation` Work
   Item and bounded Active Frame; no runtime behavior in the foundation release.
2. Schema-38-capable foundation release so it can serve as the rollback-safe
   fallback for the later behavior candidate.
3. Narrow behavior canary over the typed exact message-window lane: a full
   request creates the current Work Item; an immediate closed temporal follow-up
   such as `А вчера?` updates only its window while retaining the authorized
   conversation and role.

## Current cumulative gate

- 16,022 selected non-UI Python tests passed on 20 workers.
- One declared opt-in migration drill over copies of real operator backups was
  skipped; production backups were not used as a test playground.
- Ruff, release-surface format (862 files), mypy (206 source files), compileall,
  Bandit HIGH, JavaScript syntax and toolchain preflight passed.
- Two initial failures were stale synthetic web-kernel fixtures. `e4d4a46`
  updated them to emit the same exact query/tool identity attestation as the
  production kernel and added negative tests for missing/substituted queries;
  no production check was weakened.

## Next order

1. Complete and release the schema-38 Work Item/Active Frame foundation.
2. Release the exact message-window continuation canary with restart, expiry,
   cancellation, ownership, revision-CAS, receipt and atomic-rollback coverage.
   Generic WorkGraphs and candidate sets remain deferred.
3. Continue V12 refinement only through independently releasable, typed,
   read-only slices before considering broader effects.
4. Perform the physical Android/Syncthing-Fork evidence matrix when an actual
   device run is available; backend simulations are not recorded as physical
   certification.
5. Consider the optional companion plugin only after explicit operator approval.

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
