# Interaction Control Plane implementation status

Status: **P0A + P0B + FIRST P1 READ SLICE IMPLEMENTED AND DEPLOYED**
Date: 2026-08-23
Branch: `main`
Source: `main` / `321f8fa`
Live: Friday `0.207.4` / `6a25cda`, schema 37

## Release checkpoint

- P0A structural tracing was completed through `4ba65f1` on schema 36.
- `f99d889` introduced the schema-37-capable failure-store foundation and remains
  an available sealed schema-capable release.
- `2614e69` added the durable pre-assistant-commit failure path.
- `478378f` added the privacy-safe admin baseline.
- `ba53e2b`/`fc0c6c1` added the first typed read completion gate and
  `search_explain`; `b50e63b` made accepted outcomes durable and atomic.
- The live anchor resolves to `6a25cda`, with `b50e63b` as its current
  previous/schema-capable fallback. Backend and bridge are active, health is
  `ok`, the database is schema 37 and `PRAGMA integrity_check` is `ok`.

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
- Durable Work Items, general Active Frames and WorkGraphs have not started in
  this contour and must not be inferred from the tracing layer.

## Next implementation order

1. Extend typed outcomes to the remaining highest-value document, message and
   web read capabilities without migrating every legacy tool at once.
2. Start P2 lightweight durable Work Items and Active Frame only after the P1
   contracts and receipt boundary are stable.
3. Keep generic autonomous WorkGraphs behind those prerequisites.

## Current cumulative gate

- Focused accepted-receipt gate: 190 passed.
- Full non-UI Python gate: 15,874 passed, zero skipped, including pinned
  Syncthing `v2.1.3`.
- Toolchain preflight, Ruff, format, mypy, compileall, Bandit HIGH and JavaScript
  syntax checks passed. Docker and the separate unchanged browser/UI phase were
  deliberately outside this checkpoint.
