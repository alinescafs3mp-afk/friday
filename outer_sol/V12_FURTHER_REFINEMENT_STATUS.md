# V12 further refinement status

Status: **P1A + P1B + P1C IMPLEMENTED AND DEPLOYED**
Date: 2026-08-23
Branch: `main`
Source: `main` / `321f8fa`
Live: Friday `0.207.4` / `6a25cda`, V12 canary for `FILE_READ` and `ARCHIVE_READ`

The current authority remains:

- `docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md` for the frozen `TurnPlan v1`
  and attested file/archive canary boundary;
- Proposal 86 in `sol/PROPOSALS.md` for the continuous retrieval roadmap;
- `outer_sol/INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md` for the
  required ordering: typed outcomes before Work Items and WorkGraphs.

The historical `docs/NEW_ROUTER_MODEL_MIGRATION_PLAN.md` and the stale
`v12-model-first` branch are not implementation sources.

## Completed and deployed

### P1A: typed read outcome and publication gate (`ba53e2b`)

- Closed immutable `CapabilityOutcome v1` for the existing `FILE_READ` and
  `ARCHIVE_READ` routes.
- Closed complete/partial/empty/unavailable/denied states with reason and
  retryability invariants.
- Deterministic pre-publication gate binds route, attested plan, evidence
  identity, citations, authority recheck and verifier result.
- Only the admitted complete postcondition reaches the existing atomic assistant
  publication; malformed, mismatched or non-complete outcomes fail closed.
- Handler and real-router regressions cover outcome parsing, route/plan/evidence
  mismatch, citation mismatch, authority and retryability boundaries.

### P1B: read-only search explanation (`fc0c6c1`)

- `friday.search_explain.v1` is available through
  `GET /api/admin/retrieval/explain` behind `admin.all_data.read` and
  cross-tenant audit.
- The projection reports selected/unavailable corpora, coverage and index
  freshness, date-role semantics, candidate/discard shape and bounded rank
  signals without presenting an unsupported corpus as an empty search.
- Explanation uses the production retrieval mode without recording usage, so
  diagnostics do not change the ranking they describe.
- A preregistered difficult-query fixture and privacy/contract regressions cover
  the shipped surface.

### P1C: durable accepted-outcome receipt (`b50e63b`)

The completed bounded slice is a closed `AcceptedCapabilityOutcomeReceipt` for the
same two V12 routes:

- canonical accepted-outcome hash;
- strict attach/load validators;
- mandatory atomic persistence in private assistant metadata after the
  completion gate, followed by a durable-row reread before commit;
- a closed 65,536-byte carrier limit; size, validation, tamper or durable-reread
  failure rolls the entire message publication back;
- `ARCHIVE_READ` inherits the same binding and public HTTP/Telegram/replay
  projections strip the private receipt;
- 190 focused contract, file/archive and privacy tests passed. The cumulative
  non-UI gate then passed all 15,874 tests with zero skips.

P1C added no schema, route, model profile, web/effect capability or companion
plugin dependency.

## Boundary and next order

- `TurnPlan v1`, the attested model profile and the two-route canary boundary
  remain frozen.
- No new V12 route is implied by P1A/P1B/P1C.
- After P1C, widen typed outcomes deliberately to the next read-only capability;
  do not jump directly to effects or a generic WorkGraph.
- Durable Work Items and Active Frames remain downstream of stable typed outcome
  and accepted-receipt contracts.
