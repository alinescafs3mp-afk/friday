# Interaction Control Plane implementation status

Status: **SCHEMA-41 VERTICALS DEPLOYED; NEXT DURABLE COMPARISON JOURNEY ACTIVE**
Date: 2026-08-25
Branch: `main`
Source/live: Friday `0.207.26` / `9ab75a82393919e477890b601d243ae7baedad5a`,
tree `87f05bedd19fe76ccb5928e21b47106caac1660c0bcf4e8994f8c20967d9d2e5`,
wheel `c59c920e1936cd1cb3a386f062a1aec47a367cc4cce2767f9b148ec214ae43e1`,
schema 41; immediate predecessor and schema-capable fallback Friday `0.207.24` /
`9142765647b75d12cea22798df6782a09bc5c4b8`, tree
`ce654409f09b93cc651543968e81bb7254dd5af48d8698ae7cd06c0084d28f30`

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
- The live anchor resolves to `9ab75a8`, with schema-41 `9142765` as both
  immediate predecessor and rollback fallback. Backend and bridge are
  active, trusted-CA health is `ok`, and
  schema 41, SQLite integrity, foreign keys, FTS and body-free catalog bindings
  are clean; semantic enrichment coverage remains honestly partial.
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
  archive candidate selection. They must not be generalized into a claim that
  generic document recall, Active Frames or WorkGraphs are complete.
- The common effect contract is proven only for the released Obsidian
  create/append vertical. It is not evidence that every reminder, connector or
  future side effect has adopted the envelope.

## Next implementation order

1. Use the canonical golden-journey/evidence registry in
   `outer_sol/PROJECT_IMPLEMENTATION_STATUS.md`; its machine validator owns the
   strict readiness and evidence rules.
2. Select the next incomplete bounded ICP/V12 golden journey and close its
   deterministic, integration, artifact and recovery evidence.
3. Register product-linked candidate evidence from natural production use; do
   not create a synthetic live test corpus.
4. Keep generic autonomous WorkGraphs behind complete user journeys.

The selected next journey is `CompareConversationWithDocument`: preserve exact
selected message evidence, wait durably for a document reference, survive
restart, resolve attachment/name/ordinal under fresh authority and revisions,
then publish one independently verified two-source comparison and one atomic
receipt. Its schema-capacity reader lands before the writer/runtime package;
`TurnPlan v1` remains unchanged.

## Current cumulative gate

- The exact `0.207.24` base passed 18,666 non-UI and 31 UI tests; the pinned real
  Syncthing 2.1.3 smoke executed rather than remaining environment-skipped.
- Schema-38 migration, lifecycle/privacy, revision-CAS, restart, temporal
  continuation, receipt/plan binding and named-inventory compatibility checks
  passed.
- Ruff, mypy, compile and release diff checks passed with zero skips. The
  bounded `0.207.26` release then passed its focused secondary/release gate,
  reproduced its wheel byte-for-byte and activated `clear`; Docker and
  companion-plugin work remained outside primary Friday certification. Gate
  scratch is owned and removed by the canonical runner.
