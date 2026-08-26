# GPT-OSS semantic supervisor implementation status

- Updated: 2026-08-26
- Architecture order:
  `outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_AND_POLICY_KERNEL_ARCHITECT_BRIEF.md`
- Source phase: **P0–P4 implemented for one bounded read-only journey; P5
  dormant; P6 gate-only**.
- Rollout phase: **default-off source candidate; production assist/canary
  evidence is not accepted or committed**.
- Durable source schema: **45**. This is not a deployed release identity.
- Model roles: accepted optional GPT-OSS-20B is an untrusted planner/reviewer;
  primary Qwen 27B remains fallback, synthesis and publication owner.

This document separates source capability from rollout acceptance. Historical
pre-shadow and isolated live-shadow JSON files remain evidence for their
original P1 scope only. They are not renamed or reused as P2–P4 promotion
evidence.

## Phase summary

| Phase | Source state | Authority boundary |
|---|---|---|
| P0 | Complete | Routing/invariant audit and production baseline are body-free; no ownership change |
| P1 | Complete | `off|shadow`; proposal is validated and discarded after primary |
| P2 | Complete for one journey | Only `compare_current_file_with_current_web`; fixed read-only adapters; primary final |
| P3 | Complete for that journey | schema-45 fixed ICP WorkGraph, request-bound ingress, restart/expiry/cancel receipts |
| P4 | Complete for that journey | At most one review and one code-admitted web recovery |
| P5 | Dormant source only | Symbolic Obsidian create/append intent; no runtime consumer or effect authority |
| P6 | Gate only | Pure evidence decision; no code/config deletion surface |

No source path gives GPT-OSS a tool handle, effect handle, permission,
idempotency grant, storage writer or publication method.

## P0: frozen routing and invariants

The repository-specific audit is
`outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_ROUTING_INVARIANT_AUDIT.md`. The frozen
classes remain distinct:

| Kind | Owner |
|---|---|
| semantic guess | planner/router prompt or closed intent recognizer |
| deterministic invariant | code-owned exact lane and completion rule |
| authority | permissions, source reauthorization and capability registry |
| lifecycle/state | idempotency lease, pending durable owner and graph revision |
| publication | primary/deterministic publisher plus atomic receipt |
| legacy compatibility | one primary fallback before promoted ownership |

Exact cancel, replay, reply/ordinal, explicit mode and pending ownership remain
ahead of semantic sampling. No-double-owner, no-double-effect, primary fallback
and no-secondary-publication invariants were retained throughout P1–P4.

The source baseline builder joins body-free primary traces, supervisor events
and promoted product events. A baseline is a candidate measurement, not
promotion authority. The historical
`GPT_OSS_SEMANTIC_SUPERVISOR_PRODUCTION_BASELINE_PRE_SHADOW.json` remains a
pre-shadow record and does not satisfy promoted evidence.

## P1: contracts and discarded shadow

Implemented surfaces:

| Surface | Role |
|---|---|
| `semantic_supervisor_policy.py` | immutable shadow/assist policy and accepted profile identity |
| `supervisor_contracts.py` | closed manifest/input/proposal/review contracts |
| `capability_manifest.py` | bounded current registry projection |
| `policy_kernel.py` | pure schema, dependency, effect and budget validation |
| `execution_plan.py` | process-sealed `ValidatedExecutionPlan`; model JSON cannot mint it |
| `semantic_supervisor.py` | secret-free bounded input and strict parser |
| `semantic_supervisor_runtime.py` | primary-preserving discarded shadow |
| `supervisor_observation.py` / `supervisor_trace_join.py` | body-free structural observability |
| `supervisor_offline_evaluation.py` | deterministic synthetic regression |

Shadow policy is `gptoss20b-semantic-supervisor-v1`, SHA-256
`9f0c1e8132200a3a4416448cd2de03a4736da5e4968536d8c9e518fd5e88051a`.
It admits `MAX_STEPS=6`, `MAX_REVIEW_ROUNDS=0` and one or both closed shadow
task classes. The scheduler overlay is lowest priority, uses the already
accepted optional profile and has no execution consumer. Laptop absence,
saturation, timeout, malformed output or epoch drift affects only the discarded
attempt and never creates a second primary call.

The canonical offline evaluator remains synthetic. The historical isolated
endpoint battery of 8/8 strict proposals proves only protocol compatibility;
`GPT_OSS_SEMANTIC_SUPERVISOR_LIVE_SHADOW_EVIDENCE.json` explicitly carries no
production, canary or promotion authority.

## P2: bounded current-file/current-web assist

The only promoted task class is
`compare_current_file_with_current_web`. Recognition requires an exact
current-turn attachment, explicit current-public-web query, dialogue mode,
tools enabled, no reply/replay/voice/special lane, one successfully claimed
idempotency request and exact source/actor/conversation bindings.

The admitted fixed plan is:

```text
files.read
  -> file_read / friday.orchestration.file_read.V12FileReadHandler
web.search.current
  -> web.compare.transient /
     friday.orchestration.transient_web_comparison.TransientWebComparisonAdapter.research
primary.synthesis
  -> attested primary model
```

The file and web branches are parallel read operations. Public-web material is
transient and never enters the legacy capture/mutate `web.research` contour.
Primary synthesis is the sole final answer. Proposal/model output cannot change
adapter IDs, effect classes, dependencies, authority or publication owner.

The assist policy is `gptoss20b-semantic-supervisor-v2`, SHA-256
`534905cdaac794f485b43e25895761f1a3588ff8eabcc20527530d7f3bd4f96e`,
with exact bounds `MAX_STEPS=6`, `MAX_REVIEW_ROUNDS=1`.

## P3: durable graph and request-bound ownership

`CompareCurrentFileWebWorkGraph` v2 is a fixed graph stored by DB schema 45.
Admission atomically binds:

- exact claimed-request SHA-256 identity;
- actor, conversation and anchor user message;
- current Raw object, source identity and content digest;
- sealed plan, policy, runtime profile and adapter registry;
- fixed step input identities and idempotency keys.

The request-effect fence must match the graph admission binding in the same
transaction. A cancellation has its own exact request binding and atomic fence.
Every capability/publication boundary reloads current graph state, rechecks
authority/source identity and uses revision-aware compare-and-set.

Before ingestion, an ACTIVE graph produces one closed relation:

- `ROOT_REPLAY`: never reaches legacy;
- `NEW_TURN`: normal ingestion, exact graph reconciliation, then one primary;
- `EXPLICIT_CANCEL`: no ingestion; cancels only the bound graph;
- `UNCERTAIN`: no ingestion or fallback.

The carried relation must match current text, person, conversation and request
binding. A stale/foreign/malformed relation becomes scoped uncertainty.

Process restart invalidates process-private evidence/actor handles, so startup
terminalizes all remaining ACTIVE graphs before serving traffic. Bounded expiry
does the same for due graphs. Interrupted same-process records may be
deterministically retired; committed results are never exposed as pending or
cancelled twice. Schema44 feature fixtures migrate through an authenticated
44→45 topology with an explicit `UNBOUND_SCHEMA44` sentinel; the sentinel is
always uncertain and never fabricates replay authority.

Terminal, cancelled, expired, restart and successful publications carry typed,
body-free receipts and exactly one assistant publication. Mixed-authority
partial evidence terminalizes without leaking a usable denied branch or
falling back after ownership.

## P4: review and bounded recovery

After deterministic read completion, GPT-OSS may return one untrusted
`SupervisorReviewV1`. Code may admit only one closed read-only recovery:

- maximum one review;
- maximum one retry of the public-web read;
- no retry of file read, synthesis, effect or publication;
- fresh registry, runtime epoch, source and authority checks;
- then primary synthesis or an honest deterministic partial/terminal result.

There is no recursive planner/reviewer dialogue and no secondary publication.

## Promotion and rollback boundary

Canonical ENV remains `MODE=off`, `PROMOTION_ENABLED=0`. Source support does
not authorize live promotion. Shadow and promoted modes use independent
policies, bounds and operator transitions.

Assist/canary activation requires:

- exact source revision and registry binding;
- accepted mode-specific latency budget;
- production-joined readiness/outcome evidence with at least 20 observations;
- complete trace join and zero hidden owner, duplicate capability/effect/
  publication, false-completion and user-visible regression counts;
- explicit fallback, laptop-off, final-authority and primary-publication proof;
- exact baseline/operator provenance;
- canary-only exact actor allowlist and predecessor assist evidence chain.

Promotion evidence is a private, no-overwrite artifact produced by
`tools/build_semantic_supervisor_promotion_evidence.py`; immutable release
validation and runtime activation must independently reject legacy, malformed,
stale or incomplete evidence. The reversible operator transitions are:

```text
off <-> shadow <-> assist <-> canary
```

No promotion artifact is committed in this source candidate. A release
integrator must allocate a distinct schema-45 fallback/candidate pair, collect
real evidence, exercise every activation/recovery boundary and retain rollback.

## P5: effect planning without effect authority

`supervisor_effect_intent.py` and
`supervisor_effect_intent_transport.py` are deliberately dormant. They accept
only symbolic Obsidian note `create|append` plus manifest/proposal identities.
Model output cannot carry arguments, paths, risk, permission, confirmation,
idempotency key, tool/effect handle or publication handle. A process-sealed
code binding and fresh gate return only a non-authorizing advisory.

No production module imports or consumes this advisory. Host application,
installation, network, shell, delete and unknown symbols are rejected. P5 does
not change an effect owner and is not a hidden activation surface.

## P6: heuristic retirement remains closed

`supervisor_retirement_gate.py` is pure release-time evaluation. It can admit
only a reviewer-visible candidate for this exact journey after production
shadow, canary, promotion, fallback, joined traces, rollback, documentation and
registry evidence all pass. Deterministic, authority, lifecycle and publication
guards are structurally ineligible. The gate cannot edit code/config or delete
a branch, and this package removes no legacy heuristic.

## Source verification and release state

Dedicated source gates cover:

- contract/parser/prompt-injection and secret/path rejection;
- laptop-off, timeout, saturation, malformed response and epoch drift;
- fixed surface, sealed plan, registry/authority/source drift;
- graph admission, revision races, restart, expiry, cancellation and receipts;
- one-review/one-recovery bounds and primary-only publication;
- request replay/new/cancel/uncertain API boundaries;
- promotion evidence, latency budget and immutable transition recovery;
- dormant P5 and pure P6 non-authority contracts;
- authenticated schema migration fixtures through schema 45.

The source is ready for release integration and representative live testing
only after all repository gates are green. It is not itself evidence that
assist/canary ran in production, improved completion or stayed inside the
latency budget. Those claims must come from a new accepted production window,
not from synthetic fixtures or the historical isolated shadow battery.
