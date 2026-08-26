# Friday: canonical execution backlog

Updated: 2026-08-27

This file owns the current execution order. Architecture briefs remain design
inputs; they do not override this priority list or prove production readiness.

## Operating rules

- Release small independently reversible packages to `main` and production.
- Do not use Docker to certify primary Friday. The laptop inference node keeps
  its separate Docker contour.
- Do not touch the Obsidian companion plugin without a separate owner request.
- Preserve the primary-only path whenever the laptop or secondary runtime is
  unavailable.
- Do not weaken guards or sandboxes to make acceptance pass. Remove only a
  guard proven both harmful and redundant with a stronger code-owned boundary.
- Do not merge old feature branches wholesale. Re-audit and port only exact
  useful commits onto current `main`.

## Priority order

### P0 — production message stability

Status: deployed in `0.207.52`.

- Keep `AgentRuntime.chat` and every orchestration wrapper structurally
  signature-compatible.
- Preserve authenticated Telegram carrier identity through every legacy,
  fallback and canary branch.
- Keep signed `/api/chat` and signature-parity regressions release-blocking.

### P1 — make the current Engineer Mode a complete user workflow

Goal: close `request → approval → run → progress → result files/archive` for
installed console applications. This is battle-readiness of the already-live
runner, not the later expansion to every compiler and reverse-engineering
family.

1. Preserve truthful trusted-output refusal receipts and independently port the
   isolated fix from commit `81998fd29b38adcace8dbfe717a4d74bed4d32f3`.
2. Read only sealed job outputs, revalidate path/type/size/SHA, persist them as
   generated Raw objects and deliver files or a deterministic archive exactly
   once through Telegram.
3. Add durable terminal notification and sparse fact-based progress after
   restart. Never invent percentages or ETA.
4. Resolve natural “status/cancel current task” through an exact
   actor/conversation binding; fail closed when more than one job is plausible.
5. Add bounded retention that never removes pending, uncertain or unpublished
   output.
6. Grant current Telegram files to a confirmed job as immutable read-only input
   snapshots; bind every digest to the approval and re-authorize immediately
   before execution.
7. Wire the already-present bundle/publication seams to command, Java and patch
   flows so sources, binary/output and receipts arrive atomically.
8. Gate traversal, symlink, hardlink, race, tamper, restart, duplicate callback,
   cancellation/cgroup and nmap-route isolation; then perform one benign live
   Telegram smoke.

Estimate: first usable result-delivery slice 6–9 hours; complete P1 contour
12–20 hours of clean work. The final physical approval-button smoke is
owner-bound but does not block all preceding work.

### P2 — integrate and roll out Semantic Supervisor

Goal: use GPT-OSS as a bounded semantic supervisor/policy kernel while the
primary remains the sole publisher, tool caller and effect owner.

1. Resolve the profile dependency once: either promote and accept staged
   abliterated profile `gptoss20b-d4c220…` before Supervisor certification, or
   explicitly retain accepted `gptoss20b-2335df…` for the whole rollout. A
   later profile switch invalidates profile-bound evidence.
2. Rebase the clean source candidate from
   `feature/semantic-supervisor-full-20260826` onto current `main`; do not trust
   the revoked READY handoff.
3. Correct policy/code identity drift and add an identity-consistency test.
4. Independently review authority, ingress, privacy, schema, restart,
   Engineer/secondary interaction and primary-once publication invariants.
5. Run focused suites, static checks and the exact full gate from a clean
   artifact; reproduce candidate and schema-capable fallback wheels.
6. Release default-off, rehearse real rollback, then enable shadow.
7. Collect at least 20 joined production observations and produce latency and
   operator evidence.
8. Promote only the proved current-file-plus-current-public-web journey to
   limited assist, rehearse assist-to-shadow, then use a small canary chain.
9. Keep P5 effect work shadow-only until the canary and Obsidian registry are
   mature. P6 remains a no-op unless a heuristic is independently proved
   harmful and redundant.

Estimate: 18–30 hours after P1, plus 3–6 hours if the staged laptop profile is
certified first.

### P3 — release stability and golden-journey admission

- Make core Telegram, web, file/Office, Obsidian and reminder journeys
  release-blocking.
- Add exact clean-artifact, production-read-only, rollback and backup/restore
  evidence.
- Salvage only reviewed pieces from `feature/package6-evidence-foundation`;
  never merge that stale incomplete candidate wholesale.

Estimate: 8–16 hours.

### P4 — finish document and conversation retrieval

1. Reader-first document-passage manifest/table with schema-capable fallback.
2. Bounded writer and restart-safe resumable backfill.
3. Typed arrival/container/visible/mentioned dates with evidence spans and one
   shared query parser.
4. Full-corpus passage embeddings, revision incompatibility, resumable backfill,
   measurable recall/reranking and honest `index_incomplete`.
5. Conversation passages, adjacent context, typo/layout repair, typed dates and
   unified archive coverage.

Estimate: 31–54 hours in separately reversible releases.

### P5 — complete operational memory and recovery

- Extend typed outcomes to durable web research, document review and
  internal/external comparison; keep generic WorkGraphs behind proved journeys.
- Certify scheduled work for at-most-once delivery, restart/cancel/expiry and
  uncertain-effect recovery.
- Prove clean-home backup/restore for DB, files, inbox and Obsidian generation,
  deterministic index rebuild, pending work/effects, ENOSPC, clock skew and
  duplicate inbound delivery.

Estimate: 22–44 hours.

### P6 — Obsidian physical acceptance

- One real Android onboarding, Android-origin edit/reingest, offline reconnect,
  concurrent conflict and delivery-versus-open proof.
- Server-side P0–P7 is already implemented. Companion remains excluded.

Estimate: 1–3 owner-present hours plus any defects found.

### P7 — expand Engineer Mode after Semantic Supervisor

- Closed compiler profiles: C/C++, then Go and Zig; each returns sources,
  binary and receipt without executing the result.
- Deeper APK/JVM/.NET/Mach-O analysis and indexed Ghidra output.
- Semantic modify/rebuild/archive workflows with original preservation.
- Durable search for reports/artifacts/receipts by hash, date and target.
- Comprehensive single-host assessment. Exploit validation remains a separate
  owner-confirmed security package against an explicitly safe target.

Estimate: 24–50 hours, incrementally.

### P8 — later architecture work

- Minimal sensitive-data governance only after the owner chooses deployment,
  classification and key-recovery policy.
- Wider V12 route migration only after P4/P5 evidence; generic Active Frames,
  WorkGraphs and Executive unification remain late work.
- MCP/web edge generalization and document admin APIs follow core reliability.

Estimate: 40–80 hours after the policy decision.

## Explicitly deferred or closed

- Obsidian companion, shared/multi-device vault, desktop control and remote
  agents: deferred.
- Local coding-agent orchestrator: development tooling, not current Friday
  product convergence.
- Old WIP blobs, old V12 model-first branch and PLAN-002/004: superseded.
- Broad exploit validation: no work without separate scope and safe target.

## First 24 clean-work hours

1. Finish P0 evidence and keep production green.
2. Deliver P1 sealed output publication, current-job continuation, terminal
   notifications and retention as the first small release.
3. Deliver P1 Telegram input grants plus bundle/publication wiring as the next
   release if the first package remains green.
4. Start P2 profile decision and Semantic Supervisor rebase/audit with the
   remaining time; do not wait idly for owner-only physical checks.
