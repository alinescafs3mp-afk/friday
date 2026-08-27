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
- The owner-only autonomous Engineer Mode deliberately has no per-command HITL
  or isolated-workspace policy rail. Its hard boundary is instead fresh owner
  Telegram provenance and capability authorization; every other surface keeps
  its existing controls.
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

Status: deployed in `0.207.57`.

Goal: close `authenticated owner request → autonomous plan/tool loop → host-user
run → progress → result files/archive` for arbitrary software installed in the
Friday VM. No `/approvals` callback is part of this mode. Friday chooses and
chains its own commands, sees their real output, may use the VM filesystem and
network as the Friday service user, and keeps durable cancellation/progress and
artifact delivery.

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
6. Grant exact current-message Telegram files to a job as immutable read-only
   input snapshots; bind every digest to the request/receipt and re-authorize
   immediately before execution.
7. Wire the already-present bundle/publication seams to command, Java and patch
   flows so sources, binary/output and receipts arrive atomically.
8. Gate traversal, symlink, hardlink, race, tamper, restart, duplicate callback,
   cancellation/cgroup and nmap-route isolation; then perform one benign live
   Telegram smoke.

Items 1–2 and their traversal/symlink/hardlink/race/tamper/legacy/UNKNOWN
gates are deployed in `0.207.53`. Item 4 is deployed in `0.207.54` with durable
exact-scope focus and fail-closed ambiguity handling. Item 3 and the workspace
portion of item 5 are delivered in `0.207.56`: terminal publication is
automatic, progress is durable and fact-only, and only old proven-sent
workspaces are retired while canonical archives remain. The preceding
owner-confirmed isolated command admission is now a deployed predecessor, not
the target product contract. Autonomous owner-only host-user admission,
iterative planning, current Telegram inputs, lifecycle closure and items 6–8
are deployed in `0.207.57`. The live signed owner smoke executed an installed
command and returned exact stdout without an approval row. The P1 contour is
closed; future Engineer expansion belongs to P7.

Production regressions found after rollout are closed by `0.207.58`: composite
systemd time budgets are parsed exactly, current Telegram uploads reach the
autonomous service, an identical failed step executes once, model-authored fake
terminal messages are rejected, and terminal delivery no longer creates empty
archives. A real uploaded PE completed through the live command kernel with the
requested 300-second timeout. `0.207.59`/`0.207.60` restore stable V12 restart
attestation by widening only bounded SGLang observation budgets; same-epoch
identity, exact-zero drain and fail-closed semantics remain intact. Production
is `canary_ready` with `archive_read` and `file_read` live.

`0.207.62` removes model-selected deadlines from the arbitrary-command schema,
stops same-turn polling after durable admission, recovers terminal command truth
across provider failures and returns no-file stdout/stderr as bounded Telegram
text instead of an empty archive. Progress now exposes measured stage, elapsed
time, output byte counts and only a real hard-deadline remainder. `0.207.63`
deploys the reusable edited Telegram status surface and selective bounded
reasoning for complex plan/replan turns. Progress delivery is advisory and
cannot duplicate task execution; execution/status/final phases remain
no-thinking, and no approval rail was reintroduced.

P1B foundation is deployed dormant in `0.207.65`; remaining work is restart-safe
runtime continuation across messages. Activate one
journey-specific `EngineerWorkItem v1`, not a generic WorkGraph: persist only
owner/conversation/source identity, revision/state, code-owned step ordinal,
idempotency key and command/terminal receipt digests. A fresh authenticated
follow-up may inject the exact observed terminal receipt into one bounded
`replan → next step or final` turn. Never persist prompt/CoT/argv/output/path,
never blindly replay `RUNNING/UNKNOWN`, and publish completion atomically with
the Work Item CAS. Before activation, make the independent command ledger
fail-closed against loss/rollback and bind a code-owned source slot so dependent
same-message commands remain possible without replay ambiguity.

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

Items 1–6 are deployed through `0.207.63`. The exact accepted profile
`gptoss20b-2335df…` is retained for this rollout; production health proves
`shadow` effective but currently reports the optional secondary runtime
unavailable, despite a direct authenticated endpoint probe succeeding. Items
7–9 remain evidence-gated with zero credited production observations; shadow
has no tool, effect, execution or publication authority.

### P3 — release stability and golden-journey admission

- Make core Telegram, web, file/Office, Obsidian and reminder journeys
  release-blocking.
- Add exact clean-artifact, production-read-only, rollback and backup/restore
  evidence.
- Salvage only reviewed pieces from `feature/package6-evidence-foundation`;
  never merge that stale incomplete candidate wholesale.

The first document-budget slice is deployed in `0.207.64`: nested document,
Office and archive stages share inherited size-aware deadlines; encrypted
archive validation fails closed before dedup/persistence, while authenticated or
plain work may return an honest partial result.

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

1. Keep the deployed P0/P1 production paths green.
2. Activate the deployed dormant `EngineerWorkItem v1` through the exact
   owner-follow-up path as a separate reversible package.
3. Collect and audit P2 joined shadow observations for the frozen accepted
   profile only from real eligible turns; do not fabricate traffic.
4. Advance only through evidence-backed limited assist/canary stages, then use
   remaining time on P3 release-blocking golden journeys.
