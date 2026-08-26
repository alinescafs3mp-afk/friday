# GPT-OSS semantic supervisor implementation status

- Updated: 2026-08-26
- Architecture order:
  `outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_AND_POLICY_KERNEL_ARCHITECT_BRIEF.md`
- Phase: **P0 audit frozen; P1 default-off discarded shadow; P2 not admitted**
- Source basis: prerequisite `e40895104afd6e75d8dc8ece7eb482fe7f5c94f2`,
  runtime commits `5ad5a6163078df534fe5800ce1d30d8922411116` and
  `9f2bac68b91b9c6d5fd58f5f219ca4b9f5687753`, plus the release/evidence
  closure commit containing this status. The exact clean tip and gate receipt
  are recorded in the Sol handoff; no release identity is claimed here.
- Production identity, durable DB schema and primary model authority are
  unchanged. The accepted optional model is GPT-OSS-20B; the primary Qwen 27B
  remains the fallback, runtime answer and publication owner.
- Body-free release evidence contracts intentionally advance from product
  diagnostics v1 to v2 and stage evidence v2 to v3: endpoint request/success
  counters now mean four physical HTTP tasks (profile, models, canary, product),
  while admission remains three. Legacy evidence is rejected, not reinterpreted.

## P0: semantic guesses versus real invariants

The audit remains frozen before any ownership change.

| Kind | Current owner | Evidence |
|---|---|---|
| Semantic heuristic | `V12Planner` prompt and `TurnPlan.route` | `friday/orchestration/planner.py`; one exclusive `TurnPlan v1` route |
| Deterministic invariant | One user-visible runtime owner | `OrchestrationRouter`; effectful plans remain legacy-owned |
| Deterministic invariant | Fail-closed router mode | unknown `FRIDAY_ROUTER_MODE` becomes `legacy` |
| Deterministic invariant | No double-effect replay | no legacy retry after an effect owner starts |
| Authority | Code-owned capability/effect gates | `friday/permissions`; route handlers and publication reauthorize |
| Lifecycle/state | Pending durable Work Item admission | owned or uncertain pending turns stay on their exact lane |
| Publication | Primary/deterministic publisher | optional secondary output never publishes |
| Optional secondary | Advisory product workloads | scheduler admission is independent of route/effect authority |

Frozen invariants:

- no second runtime, effect or publication owner;
- laptop-off preserves one unchanged primary path;
- GPT-OSS output remains untrusted data;
- exact cancel, ordinal, replay and pending-state lanes precede semantic
  sampling;
- no proposal may construct authority by naming a capability or risk class.

## P1 implementation surfaces

| Surface | P1 role |
|---|---|
| `friday/semantic_supervisor_policy.py` | Immutable product-policy/profile identity and fail-closed scheduler admission |
| `friday/orchestration/supervisor_contracts.py` | Closed manifest, input, proposal and review data contracts |
| `friday/orchestration/capability_manifest.py` | Bounded code-owned per-turn capability projection |
| `friday/orchestration/policy_kernel.py` | Schema, manifest, task-shape, dependency, effect and budget validation |
| `friday/orchestration/execution_plan.py` | Kernel-sealed `ValidatedExecutionPlan`; model JSON parsing is forbidden |
| `friday/orchestration/semantic_supervisor.py` | Eligibility, body-bounded secret-checked prompt and strict proposal validation |
| `friday/orchestration/semantic_supervisor_runtime.py` | Non-owning primary-preserving shadow sidecar |
| `friday/server.py` | Admitted sidecar construction, top-level body-free health and ordered shutdown |
| `friday/secondary_brain/scheduler.py` | Independently admitted `plan_candidate` overlay, always effective shadow |
| `friday/orchestration/supervisor_observation.py` | Body-free structural observation contract |
| `friday/orchestration/supervisor_offline_evaluation.py` | Pure synthetic fixture replay; no network or capability operation |
| `tools/evaluate_semantic_supervisor_offline.py` | Canonical body-free offline report command |
| `docker-compose.yml`, `friday/cli.py` | Default-off semantic configuration surface |
| `tools/immutable_release_operator.py` | Exact five-key shadow enable/disable candidate transitions |

`ValidatedExecutionPlan` is sealed by the Policy Kernel and cannot be created by
parsing model JSON. P1 may validate such a candidate in memory, but the sidecar
has no execution consumer and discards the result.

## Exact admission and modes

Default state:

```text
MODE=off
TASKS=empty
MAX_STEPS=6
MAX_REVIEW_ROUNDS=1
TIMEOUT_SEC=12
```

Unknown mode, empty/invalid task allowlist, invalid bounds, secondary disabled,
public-text-only admission, profile mismatch, unaccepted runtime or policy drift
all close the overlay to `off` and preserve the original runtime object.

P1 admission requires all of:

- exact accepted profile
  `gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`;
- exact accepted profile-manifest SHA-256
  `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`;
- `FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1` and otherwise admissible exact
  secondary TLS/profile configuration;
- product policy `gptoss20b-semantic-supervisor-v1`, SHA-256
  `9f0c1e8132200a3a4416448cd2de03a4736da5e4968536d8c9e518fd5e88051a`;
- one or both exact task classes
  `compare_current_file_with_current_web` and
  `compare_archive_with_current_web`.

`plan_candidate` is a code-owned overlay and is deliberately removed from the
generic ENV workload list. The supervisor policy reuses the accepted model
runtime; it does not recertify it.

The immutable P1 enable candidate changes only the semantic block to:

```text
MODE=shadow
TASKS=compare_archive_with_current_web,compare_current_file_with_current_web
MAX_STEPS=6
MAX_REVIEW_ROUNDS=0
TIMEOUT_SEC=12
```

The disable candidate restores the exact default block. The transitions are
`semantic_supervisor_shadow_enable` and
`semantic_supervisor_shadow_disable`. Enable accepts only canonical exact off or
the legacy exact absence of all five semantic keys; the latter is safe only
because the already-installed predecessor source reports absent ENV as
uninstalled/effective off. Partial and unknown semantic blocks are rejected,
and pre-backup rollback/recovery preserves legacy absence byte-for-byte. Their
secondary prerequisite is the exact current
accepted private secondary production state (`ENABLED=1`,
`ALLOW_PRIVATE_TEXT=1`, `MODE=assist`, `WORKLOADS=document_map,extract`,
`DOCUMENT_MAP_MODE=assist`, exact finalist model/profile/timeouts, API-key shape
and accepted CA digest). Neither transition changes any secondary or unrelated
ENV byte. The staged owner-private mode-0600 file has a closed layout: unchanged
unrelated bytes, five lexically sorted semantic keys, then the existing
canonical sorted secondary block; semantic append after the secondary block is
rejected.

`shadow`, `assist` and `canary` are accepted requested labels, but every
non-off label has `effective_mode=shadow` and `promotion_admitted=false` in P1.
There is no assist/canary authority path.

## Live shadow semantics

The sidecar prepares a bounded projection before the primary await, invokes the
existing primary exactly once and returns the exact primary response object.
Only after a successful primary result may it schedule discarded background
work.

The shadow path is bounded by:

- at most four pending attempts;
- the original turn deadline and a 0.1–15 second semantic timeout;
- one 512-token `plan_candidate` response;
- exactly 3,328 UTF-8 input bytes for the accepted 4K adapter envelope;
- a 256-observation in-memory body-free ring;
- no review round, execution round or recovery loop.

P1 scheduler admission accepts only the runtime shape it actually implements:
`max_steps=6` and `max_review_rounds=0`. Manual steps `1`/`2` (or any value
other than `6`) and review `1` close `plan_candidate` with `invalid_bounds`.
The input builder chooses the longest character-safe prefix within the exact
`4096 - 512 - 256 = 3328` byte budget, then the request builder repeats the
same preflight. Full-source secret, path and private-ID classification happens
before projection through fixed body-free denial markers, so a prohibited
suffix cannot be truncated into admission.

`plan_candidate` is the lowest-priority user of the already accepted
single-concurrency endpoint. Any pre-existing workload atomically displaces a
semantic attempt, waits for cancellation-safe permit release and then retains
the original admission behavior. A malformed, truncated, reasoning-bearing or
policy-rejected semantic body is accounted only to `plan_candidate`; it cannot
open the shared circuit or invalidate the accepted epoch for foreground
document/extraction advice.

A synchronous late guard runs after endpoint admission and immediately before
the HTTP task is created. It rechecks the exact pending-turn owner, deadline and
same-conversation epoch without an intervening await; a newer turn or pending
state therefore closes the shadow before private dispatch. Turns exceeding the
upstream authoritative `TurnInput` bound fail closed; valid longer turns use
only the byte-bounded supervisor projection and never alter the primary input.
Such a pre-dispatch close is recorded as `invoked=false` and endpoint
`not_called`; admitted permits and actual HTTP request/success counters are
separate. Superseded and shutdown-cancelled attempts retain a body-free
observation, while sidecar shutdown bounds a cancellation-resistant evaluator
to one second before primary cleanup continues.

It bypasses owned/uncertain pending turns and all conservatively special
surfaces, including cancel/ordinal, replay/reply, explicit mode, voice,
synthetic document notice and disabled tools. The only admitted ingestion
projections are exact closed transient `web_request` and
`archive_search_request` shapes plus an exact synthetic `system_notice`; every
other ingestion result fails closed to bypass. Ordinary dialogue, small talk
and established single-file reads remain on their existing primary/exact path.

The proposal cannot:

- select or replace the authoritative route;
- alter the primary prompt or response;
- execute `file`, `archive` or `web` steps;
- create or update a Work Item;
- call a tool, start an effect or write knowledge;
- persist a model body or publish any output.

Laptop unavailable, sleeping, unhealthy, saturated, timed out or malformed
therefore means a closed/skipped background attempt after the unchanged primary
path. It must not become startup failure, a second primary invocation or an
alternate-runtime replay.

## Body-free health/metrics and offline evidence

`/api/health.secondary.semantic_supervisor` exposes only closed scheduler
policy/availability fields. Top-level `/api/health.semantic_supervisor` exposes
`installed`, discarded-shadow role, requested/effective mode,
`promotion_admitted=false`, unchanged runtime/primary publication ownership and
the explicit tools/effects/execution prohibitions. An installed sidecar also
exposes policy/profile identity, pending bounds and bounded
observation/invocation/skip/parse/policy counts. Owner diagnostics adds only
bounded `plan_candidate` routing, availability/reason and scheduler counters.
No projection may contain message/document/query bodies, raw
prompts/proposals/model output, paths, actor/conversation identifiers or
endpoint exception text.

Canonical offline replay:

```bash
.venv/bin/python -I -B tools/evaluate_semantic_supervisor_offline.py \
  --fixtures tests/fixtures/semantic_supervisor_offline_v1.json
```

The expected report is deterministic and body-free. It drives the real
`SemanticSupervisorShadowRuntime` through an in-memory primary and proposal
adapter; fixtures no longer claim a `primary_trace`. The harness measures ten
primary calls, exact response-object identity and value, shadow-after-primary ordering,
10/10 runtime invariant conformance, two exact-lane bypasses, two valid
proposals, stale/unknown/malformed rejection coverage, and zero instrumented
execution/publication/effect tripwire counts. It installs no network endpoint.

This report explicitly has:

```text
network_used=false
live_shadow_evidence=false
live_canary_evidence=false
promotion_evidence=false
acceptance_authority=none
warning=synthetic_offline_only_not_live_shadow_or_canary_acceptance
```

It is a source regression only. It is not a live shadow/canary checkpoint,
promotion receipt or P2 acceptance.

An isolated live protocol battery against the accepted endpoint then returned
8/8 valid, strictly admitted three-step proposals: four current-file/current-web
and four archive/current-web cases. Prompt envelopes were 2,695--2,697 UTF-8
bytes; no raw response, prompt, identifier or evidence body was emitted or
retained, and execution/effect/publication counts remained zero. The canonical
body-free aggregate is
`outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_LIVE_SHADOW_EVIDENCE.json`.
It did not change production configuration and is not a production shadow,
canary or promotion receipt.

## Production wiring checkpoint: landed

The server constructs the semantic wrapper only after the scheduler admits the
exact shadow workload. It retains the underlying orchestration agent
separately, so `/api/health.orchestration.installed_mode` still reports the
actual router rather than the wrapper. Top-level semantic health reports whether
the sidecar was installed. Shutdown closes sidecar tasks first and then closes
the underlying `OrchestrationRouter`; the wrapper never takes its lifecycle
ownership or double-closes it.

Targeted server tests cover default-off health, admitted installation, retained
router mode, restored Telegram mode provenance and ordered shutdown. The
immutable operator now binds semantic enable/disable candidates to both health
projections: enable requires an installed authority-free shadow plus the exact
admitted scheduler policy/profile identity, while disable requires an
uninstalled effective-off seam. Laptop runtime availability may remain false;
the source wiring may not. These source-level gates do not themselves claim a
production endpoint activation.

## P2 remains closed

The two admitted task-class names authorize only P1 shadow classification and
proposal validation. They do not authorize execution of `web.search.current`.

The current production web contour is not an already proven pure read adapter:
its code-owned plan declares `risk=mutate`, and the journey persists accepted
outcome/publication metadata. Promoting the architect brief's
`compare_current_file_with_current_web` candidate would therefore cross a real
mutation/persistence boundary.

P2 requires a separate implementation package, an explicit authority and
persistence design, representative live shadow/canary evidence and a distinct
promotion gate. Synthetic fixtures or the accepted P1 profile cannot supply
that evidence.

## Isolation and non-changes

This P1 package does not change the durable DB schema, release version, primary
model profile, V12 route ownership, ICP durable stores, file/archive handlers,
effect ownership or publication ownership. It does not execute web research,
mutate product data or alter the accepted secondary runtime manifest. It does
change only the body-free release evidence contract to diagnostics v2/stage v3
so physical endpoint counts cannot be mistaken for the legacy logical count.
