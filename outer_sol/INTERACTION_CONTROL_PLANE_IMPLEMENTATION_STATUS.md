# Interaction Control Plane implementation status

Status: **P0A + P0B + P1 READ SLICES + NARROW P2 RECALL CANARY DEPLOYED**
Date: 2026-08-23
Branch: `main`
Source: `main` / `c91260d`
Live: Friday `0.207.4` / `c91260d`, schema 38

## Release checkpoint

- P0A/P0B structural tracing, failure storage and the privacy-safe admin
  baseline remain deployed.
- Typed `FILE_READ`, `ARCHIVE_READ` and bounded `WEB_READ` outcomes use a
  deterministic completion gate and atomic private accepted-outcome receipts.
- `0f47870` introduced the dormant schema-38 Work Item foundation; `cb1b3f7`
  established it as the sealed schema-capable fallback.
- `d1c5d6f` added the exact message-window continuation canary; `4b6bc49`
  preserved the existing named-inventory follow-up lane and `c91260d` is the
  current deployed source.
- The live anchor resolves to `c91260d`, with `cb1b3f7` as fallback. Backend and
  bridge are active, trusted-CA health is `ok`, and schema 38, SQLite quick-check,
  foreign keys and exact Work Item DDL are clean.

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
- The only durable Work Item currently released is the narrow
  `RecallConversation` exact-window canary. It must not be generalized into a
  claim that document recall, generic Active Frames or WorkGraphs are complete.

## Next implementation order

1. Add the canonical golden-journey/evidence registry with strict readiness
   states and mechanically linked proof.
2. Establish stable retrieval and passage identity plus honest coverage, then a
   read-only federated `archive_search` facade.
3. Extend durable recall to documents/messages only through those stable
   references; keep generic autonomous WorkGraphs behind that proof.

## Current cumulative gate

- Full non-UI Python gate: 16,175 passed, zero skipped, including pinned
  Syncthing `v2.1.3` managed-REST smoke.
- Schema-38 migration, lifecycle/privacy, revision-CAS, restart, temporal
  continuation, receipt/plan binding and named-inventory compatibility checks
  passed.
- Toolchain preflight, Ruff, format, mypy, compileall, Bandit HIGH and JavaScript
  syntax checks passed. Docker and the separate unchanged browser/UI phase were
  deliberately outside this checkpoint.
