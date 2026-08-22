# Interaction Control Plane implementation status

Status: **P0A IMPLEMENTED / RELEASE VERIFICATION IN PROGRESS**
Date: 2026-08-22  
Branch: `feature/interaction-control-plane-v2`
Base: Friday `0.207.4` / `3cc5cdf`

Implemented in this release slice:

- closed immutable privacy-safe `TurnTrace v1`;
- installation-local domain-separated HMAC identifiers;
- honest token/call accounting coverage;
- legacy and V12 file publication traces stored atomically in owned assistant metadata;
- publication is scoped honestly as `assistant_committed`, not HTTP/Telegram delivery;
- tracing is best-effort and omitted before it can break an answer or attachment-lineage metadata budget;
- trace fields are stripped from HTTP/Telegram projections and idempotency caches;
- closed outcomes for Obsidian, document, message, entity, web, file and reminder capability classes;
- restart linkage, continuation, concurrency, privacy and fail-closed contract tests;
- no schema change and no runtime-event retention coupling.

Explicit boundary:

- P0A observes the legacy mainline and V12 file turns that reach a durable
  assistant row; complete route coverage remains P0B work;
- failures before assistant commit are intentionally not written into `runtime_events`;
- P0B needs a dedicated user-scoped/deletion-scoped failure store before those failures can be retained;
- this is the first safe observability layer, not the complete P0-P9 control plane.

Next implementation order:

1. P0B user-scoped failure traces and episode-level baseline reports.
2. P1 typed `CapabilityOutcome` adapters for document, message and web reads.
3. P2 durable Work Items and Active Frame only after P0/P1 contracts stabilize.
4. Use the next schema revision after current schema 36; do not reuse 36.

This slice is intentionally bounded and does not claim the full P0-P9 architecture.
