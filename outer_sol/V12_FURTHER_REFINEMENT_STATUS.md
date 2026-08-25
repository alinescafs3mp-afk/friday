# V12 further refinement status

Status: **P1A + P1B + P1C + SELECTED-EVIDENCE EXPLANATION DEPLOYED**
Date: 2026-08-25
Branch: `main`
Source/live: `main` / `9ab75a82393919e477890b601d243ae7baedad5a`
Live: Friday `0.207.26`, schema 41; V12 canary remains scoped to `FILE_READ`
and `ARCHIVE_READ`; current schema-capable fallback
`9142765647b75d12cea22798df6782a09bc5c4b8`

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

### Selected-evidence explanation (`0.207.21`)

- A durable selected archive source can be explained through one attested V12
  lease, bounded exact passage projection, synthesis and independent verifier.
- Exact SourceRef/PassageRef identity, authority, source revision and lease are
  rechecked before atomic publication; every accepted fact carries nested
  passage citations and a reloadable private outcome receipt.
- Endpoint, synthesis, verifier, lease or source failure retains the exact
  structural replay. It never retries through the ordinary model or widens
  effect/tool authority.
- The canonical cross-feature product battery is now release-blocking. The
  exact release gate passed 18,454 non-UI and 31 UI tests.

## Boundary and next order

- `TurnPlan v1`, the attested model profile and the two-route canary boundary
  remain frozen.
- No new V12 route is implied by P1A/P1B/P1C.
- Select the next incomplete golden journey from the canonical registry and
  widen only the typed outcome/Work Item semantics required by that journey.
- Generic Active Frames and WorkGraphs remain behind proven bounded journeys.

The selected next journey is `CompareConversationWithDocument`. It keeps
`TurnPlan v1` frozen: code owns the two-source plan, durable continuation and
completion; one tools-disabled attested V12 synthesis plus an independent
verifier may only express the exact freshly reauthorized message/document
evidence. Reader-compatible schema capacity lands before the runtime writer.
