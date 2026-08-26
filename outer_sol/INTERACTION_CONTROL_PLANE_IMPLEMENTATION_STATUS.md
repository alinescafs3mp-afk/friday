# Interaction Control Plane implementation status

Status: **LOCATE/SELECT/EXPLAIN DEPLOYED; PASSAGE READER PACKAGE NEXT**
Date: 2026-08-27
Branch: `main`
Source/live: Friday `0.207.51` / `4b9b48adf1462cfc9af4f81c7158078ec3aab20a`,
tree `6a534f83cf030bb0beab856a4748abefcacca1b77f975c69ee2b0504451c8dd0`,
wheel `6f0a08312ac8dd5d815004027e442e19ba96d648477990d2e54557701dceb529`,
schema 43; immediate predecessor and schema-capable fallback Friday `0.207.50` /
`49cca50906dddebbefbd0d5842e193e741e06957`, tree
`c6673eea90974defc14b4f106a1011bb7182435e3a775cba70f513c7c33e2d25`,
wheel `febea38bdb586c2cd3162f7ecc66978d55579ae481d17cde2b4bef901e94aac0`.
Immutable activation is `clear`; terminal receipt
`c508dc468a83d662fa57e23142d344d95128cd849416a1806fbf14148adfe507`.

## Release checkpoint

- P0A/P0B structural tracing, failure storage and the privacy-safe admin
  baseline remain deployed.
- Typed `FILE_READ`, `ARCHIVE_READ` and bounded `WEB_READ` outcomes use a
  deterministic completion gate and atomic private accepted-outcome receipts.
- `0f47870` introduced the dormant schema-38 Work Item foundation; `cb1b3f7`
  established its first sealed schema-capable fallback.
- `d1c5d6f` added the exact message-window continuation canary; `4b6bc49`
  preserved the existing named-inventory follow-up lane. The later `2b197e1`
  checkpoint superseded `c91260d`; both are now historical.
- `0.207.8` adds the authorized read-only federated `archive_search` foundation:
  stable source/passage identity, explicit per-corpus coverage, process-private
  carriers and same-transaction reauthorization/publication.
- The live anchor resolves to `4b9b48a`, with schema-43 `49cca50` as both
  immediate predecessor and schema-capable fallback. Backend and bridge are
  active, trusted-CA health is `ok`, and schema 43, SQLite integrity and foreign
  keys are clean; semantic enrichment coverage remains honestly partial.
- The `912dc1a` schema-39 vertical is now deployed in `0.207.9`: one durable,
  body-free selected archive evidence continuation. Exact restart replay
  performs fresh authority and revision checks without search or model use;
  late denial and source drift suspend source-free. Broader ICP implementation
  now has restart, late-denial and source-drift evidence across document and
  message lanes.
- `0.207.14` deploys only the schema-capable candidate-set/question foundation.
  `0.207.15` activates its audited runtime: exact ordinal replay, restart,
  expiry, cancellation, stop/mode precedence, late denial, source drift,
  replacement and CAS races are closed without a second search or model call.
- `0.207.16` deploys the exact body-free DocumentCatalog schema and bounded
  keyset storage APIs. Production migration seeds every live file explicitly
  `backfill_pending`; no hidden startup/request-path corpus work is performed.
- `0.207.18` deploys the bounded durable enrichment/reconciliation worker and
  archive consumer. Its hotfix reclaims measured unused fair reservations
  without widening the global bound or hiding failures. The first two
  production ticks backfilled 46 and 38 rows with zero phase failures; coverage
  remains honestly partial while converging.
- `0.207.19` deploys a closed privacy-safe effect outcome and accepted receipt
  for Obsidian create/append. Publication is atomic with the assistant message;
  accepted results are immutable, and prepared/uncertain effects reconcile by
  exact observation without replaying or rewriting the vault. Sync, re-ingest
  and physical-device observations remain separate facts.
- `0.207.20` deploys the explanation-receipt reader without a writer.
  `0.207.21` activates a closed selected-evidence explanation: two attested V12
  calls, exact nested passage citations, independent verification, final
  authority/source/lease rechecks and one atomic receipt/Work-Item CAS. Any
  optional-lane failure publishes the exact structural replay instead.
- `CompareConversationWithDocument` was completed separately in `0.207.29`; it
  is not the current implementation task.
- `0.207.49` deployed the dormant reader for promotion of an accepted archive
  candidate into selected evidence. `0.207.50` atomically completes the ordinal
  candidate question, promotes the exact source/passages/coverage/receipt and
  supports a later natural explanation after a second runtime restart without
  another search or ordinary-model fallback. The reader-first rollback was
  proven against the writer's rows.
- `0.207.51` deploys only the body-free document-passage projection contract.
  Exact authoritative source revalidation was made mandatory before release;
  there is no schema, persistence writer, backfill or runtime-route change.

## P0A implemented

- Closed immutable privacy-safe `TurnTrace v1`.
- Installation-local, domain-separated HMAC identifiers.
- Honest token/call accounting and closed capability outcomes for the observed
  Obsidian, document, message, entity, web, file and reminder paths.
- Legacy and V12 file publication traces stored atomically in owned assistant
  metadata; publication means `assistant_committed`, not HTTP/Telegram delivery.
- Trace fields stripped from public HTTP/Telegram projections and idempotency
  caches. Tracing remains best-effort and cannot break the user response.
- Restart, continuation, concurrency, privacy and fail-closed contract coverage.

## P0B implemented

- Schema 37 adds a dedicated user-scoped and conversation-scoped
  `interaction_failure_traces` store for failures before assistant commit.
- Failure traces contain only closed structural fields and HMAC identifiers: no
  prompt, body, path, query, provider payload or raw exception text.
- Retention is bounded by TTL, per-user and global caps. Account deletion and
  conversation deletion count and remove their owned rows.
- `/api/chat` and regenerate establish the request scope; conversation/message
  ownership hooks and route/stage observations cover the admitted legacy/V12
  paths without borrowing `runtime_events`.
- `interaction_episode_baseline` aggregates committed and precommit turns into
  counts for intent, completion, publication, failure stage/reason, route and the
  ambiguity/partial/state-restored/authority-rechecked signals.
- `GET /api/admin/eval/interaction-episode-baseline` exposes only that bounded
  aggregate. It requires `admin.all_data.read`, a canonical target user, optional
  canonical UTC `since`, a bounded limit, cross-tenant audit, and returns 404 for
  a missing user. Raw traces, bodies and digests have no response field.

## Explicit boundary

- P0A/P0B are the completed structural observability baseline, not the complete
  P0-P9 control plane.
- P0B records a structural failure only when no owned assistant row committed;
  committed traces remain attached to the assistant row.
- The V12 `FILE_READ`/`ARCHIVE_READ` `CapabilityOutcome v1`, completion gate and
  atomic accepted-outcome receipt are deployed. The wider P1 adapter set is not
  complete; see `outer_sol/V12_FURTHER_REFINEMENT_STATUS.md`.
- The durable Work Items currently released are the narrow
  `RecallConversation` exact-window canary and the body-free
  `RecallSelectedArchiveEvidence` continuation plus the bounded body-free
  archive candidate selection and atomic selected-evidence promotion. They must
  not be generalized into a claim that cross-lane document recall, Active
  Frames or WorkGraphs are complete.
- The common effect contract is proven only for the released Obsidian
  create/append vertical. It is not evidence that every reminder, connector or
  future side effect has adopted the envelope.

## Next implementation order

1. Use the canonical golden-journey/evidence registry in
   `outer_sol/PROJECT_IMPLEMENTATION_STATUS.md`; `document_recall_answer` remains
   `DEGRADED` with `cross_lane_coverage_missing`.
2. Deploy reader-first document-passage manifest/table capacity with a
   schema-capable fallback and no writer.
3. Activate the bounded writer and resumable backfill separately, then add
   typed dates and embeddings without widening the frozen V12 routes.
4. Keep generic autonomous WorkGraphs and the separately owned Semantic
   Supervisor on `HOLD`. Docker and companion work are out of scope.

## Current focused gate

- The `0.207.50` writer package passed 75 focused foundation/schema tests and 7
  focused runtime tests, including both runtime restarts and compatibility with
  the deployed `0.207.49` reader. Both release wheels reproduced byte-for-byte
  and both activations completed `clear`.
- The `0.207.51` contract passed 59 focused passage-projection tests after the
  exact-source revalidation fix. Its wheel reproduced byte-for-byte and
  activation completed `clear`.
- Only these focused package gates are asserted here. No Docker or
  companion-plugin work entered certification.
