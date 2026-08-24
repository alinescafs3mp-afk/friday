# Interaction Control Plane implementation status

Status: **DEPLOYED CANARY + SCHEMA-39 ARCHIVE-EVIDENCE VERTICAL DEPLOYED; BROADER ICP PAUSED**
Date: 2026-08-24
Branch: `main`
Source: `main` / `1e3834dd5d987f84c6ca6a490c0cd9b3ac2756ed`
Live: Friday `0.207.9` / `2b197e1e467e93a085a1b4cc330fbda8b5b7b982`,
schema 39; fallback `f1426ca561f8914574cebf3a69f8dde83f79b568`

## Release checkpoint

- P0A/P0B structural tracing, failure storage and the privacy-safe admin
  baseline remain deployed.
- Typed `FILE_READ`, `ARCHIVE_READ` and bounded `WEB_READ` outcomes use a
  deterministic completion gate and atomic private accepted-outcome receipts.
- `0f47870` introduced the dormant schema-38 Work Item foundation; `cb1b3f7`
  established its first sealed schema-capable fallback.
- `d1c5d6f` added the exact message-window continuation canary; `4b6bc49`
  preserved the existing named-inventory follow-up lane. The later live source
  is `2b197e1`; `c91260d` is only a superseded historical checkpoint.
- `0.207.8` adds the authorized read-only federated `archive_search` foundation:
  stable source/passage identity, explicit per-corpus coverage, process-private
  carriers and same-transaction reauthorization/publication.
- The live anchor resolves to `2b197e1`, with `f1426ca` (`0.207.9`, schema 39)
  as fallback. Backend and bridge are active, trusted-CA health is `ok`, and
  schema 39, SQLite quick-check, foreign keys and exact Work Item DDL are clean.
- The `912dc1a` schema-39 vertical is now deployed in `0.207.9`: one durable,
  body-free selected archive evidence continuation. Exact restart replay
  performs fresh authority and revision checks without search or model use;
  late denial and source drift suspend source-free. Broader ICP implementation
  remains paused while the urgent optional-secondary-brain package is active.

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
  `RecallSelectedArchiveEvidence` continuation. They must not be generalized
  into a claim that generic document recall, Active Frames or WorkGraphs are
  complete.

## Next implementation order

1. Use the canonical golden-journey/evidence registry in
   `outer_sol/PROJECT_IMPLEMENTATION_STATUS.md`; its machine validator owns the
   strict readiness and evidence rules.
2. Use the now-deployed retrieval identity, honest coverage and federated
   `archive_search` foundation to extend durable recall across documents and
   messages through exact current references.
3. Add durable candidate selection and pending questions only after that narrow
   vertical proves restart, expiry, authority and revision behavior; keep generic
   autonomous WorkGraphs behind the proof.

## Current cumulative gate

- The latest completed full non-UI gate before the secondary soak-protocol-only
  follow-ups passed 17,951 tests; one real-Syncthing case remained explicitly
  environment-gated.
- Schema-38 migration, lifecycle/privacy, revision-CAS, restart, temporal
  continuation, receipt/plan binding and named-inventory compatibility checks
  passed.
- Ruff, compile and release diff checks passed. The `0.207.9` wheel reproduced
  byte-for-byte and immutable activation completed `clear`; Docker and
  companion-plugin work remained outside the primary release checkpoint.
