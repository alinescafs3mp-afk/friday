# V12 further refinement status

Status: **SELECTED-EVIDENCE RUNTIME + DOCUMENT-PASSAGE CONTRACT DEPLOYED**
Date: 2026-08-27
Branch: `main`
Deployed source/live: `4b9b48adf1462cfc9af4f81c7158078ec3aab20a`
Live: Friday `0.207.51`, tree
`6a534f83cf030bb0beab856a4748abefcacca1b77f975c69ee2b0504451c8dd0`, wheel
`6f0a08312ac8dd5d815004027e442e19ba96d648477990d2e54557701dceb529`,
schema 43; V12 canary remains scoped to `FILE_READ` and `ARCHIVE_READ`.
Immediate predecessor and schema-capable fallback: Friday `0.207.50` /
`49cca50906dddebbefbd0d5842e193e741e06957`, tree
`c6673eea90974defc14b4f106a1011bb7182435e3a775cba70f513c7c33e2d25`, wheel
`febea38bdb586c2cd3162f7ecc66978d55579ae481d17cde2b4bef901e94aac0`.
Immutable activation is `clear`; terminal receipt
`c508dc468a83d662fa57e23142d344d95128cd849416a1806fbf14148adfe507`.

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
- 190 focused contract, file/archive and privacy tests passed.

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
- The canonical cross-feature product battery is release-blocking.

### Durable locate, choose and explain (`0.207.49`–`0.207.50`)

- `0.207.49` deployed the selected-evidence promotion reader before any writer.
  `0.207.50` then atomically promotes an exact ordinal choice into that reader,
  retaining source, passage, coverage and accepted-replay identity.
- Choice and a later natural selected-document explanation survive separate
  runtime restarts, perform fresh authority/source checks and neither repeat the
  archive search nor fall through to the ordinary model. The rollback reader
  was proven against the writer's rows.
- This is distinct from `CompareConversationWithDocument`, which was already
  deployed in `0.207.29`.

### Body-free document-passage projection (`0.207.51`)

- The storage-independent private contract freezes exact Raw version/content
  identity, extracted-text identity, the code-owned passage-policy revision and
  at most 64 body-free half-open passage locators with exact slice digests.
- The source-revalidation release blocker was fixed before release: loading a
  `current` projection now requires the exact authoritative version, content
  digest and extracted text and rejects any policy, coordinate or slice drift.
- The release adds no schema, table, writer, backfill, runtime route, model
  profile or policy-kernel authority.

## Boundary and next order

- `TurnPlan v1`, the attested model profile and the two-route canary boundary
  remain frozen.
- No new V12 route is implied by P1A/P1B/P1C.
- Deploy reader-first passage manifest/table capacity before enabling its
  writer; then activate bounded resumable backfill and add typed dates and
  embeddings as separate packages.
- Generic Active Frames and WorkGraphs remain behind proven bounded journeys.
- The separately owned Semantic Supervisor remains on `HOLD`. Docker and
  companion-plugin work are not part of this sequence.

## Current focused gate

- `0.207.50` passed 75 focused foundation/schema tests and 7 focused runtime
  tests; `0.207.51` passed 59 focused passage-projection tests after exact-source
  revalidation became mandatory.
- The release wheels reproduced byte-for-byte and immutable activations
  completed `clear`. Only these focused package gates are asserted here.
