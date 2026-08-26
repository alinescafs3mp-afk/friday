# GPT-OSS semantic supervisor: routing and invariant audit

- Document ID: `FRIDAY-SUPERVISOR-AUDIT-001`
- Date: 2026-08-26
- Scope: repository source at the semantic-supervisor continuation branch
- Evidence policy: body-free structural evidence only

This register separates semantic guesses from rules that protect authority,
state, effects, and publication.  A future heuristic-retirement gate may remove
only rows classified as **semantic** and only for the exact promoted journey.
Every other row is an invariant and remains code-owned.

## Ownership and guard inventory

| Surface | Repository owner | Class | Frozen rule / failure meaning |
|---|---|---|---|
| HTTP/chat ingress and request fence | `friday/server.py`, request idempotency helpers | lifecycle | Authentication, normalization, request identity, and the turn deadline precede all model routing. |
| Persisted conversation mode | `AgentRuntime.chat` | state / authority | The current owned conversation is read before mode admission; an explicit or persisted Engineer mode is freshly authorized. |
| Pending durable turn admission | `pending_durable_turn.py`, `AgentRuntime.pending_durable_turn_admission`, `OrchestrationRouter` | state / lifecycle | Owned or uncertain Work Item state precedes V12 and supervisor sampling. Uncertainty retains the existing owner. |
| Cancel and ordinal continuation | archive-candidate parsers and pending Work Item stores | deterministic | Exact cancel/selection is never delegated to a semantic planner. |
| Reply, replay, voice, explicit mode, synthetic notice | `OrchestrationRouter`, `SemanticSupervisorShadowRuntime` | deterministic / lifecycle | Existing special surfaces bypass the supervisor; their current owner is preserved. |
| Turn-level route proposal | `V12Planner`, `TurnPlan v1` | semantic | One bounded advisory single-route guess; it does not grant capability or publication authority. |
| V12 route admission | `OrchestrationRouter` | deterministic / authority | Only registered read handlers may be promoted. Effects and unknown routes remain legacy-owned. No selected-handler exception is replayed through legacy. |
| Supervisor task classification | `semantic_supervisor.classify_supervisor_task` | semantic | Bounded cue-based eligibility for approved multi-source task classes; never a security decision. |
| Supervisor proposal | `SupervisorProposal v1` | semantic | Untrusted decomposition and criteria. It cannot execute, authorize, publish, or construct a validated plan. |
| Capability availability | operational capability binding and permission registry | authority / lifecycle | A symbolic capability is advertised only when its exact adapter, security ID, effect class, and current authorization are present. |
| Policy admission | `policy_kernel.py` | authority / deterministic | Exact manifest, schema, graph shape, projection, effect, budget, and continuation checks create the only admitted plan. Model risk hints are advisory. |
| Secondary endpoint/profile admission | `secondary_brain`, `semantic_supervisor_policy.py` | lifecycle / authority | Exact accepted profile, product policy, health epoch, deadline, and concurrency-one admission. Laptop absence is a bounded skip. |
| Current file evidence | `file_evidence_reader.py` | authority / state | Owned source identity, content digest, extraction receipt, actor permission, and on-disk bytes are checked at prepare and publication. Drift fails closed. |
| Archive/message evidence | archive/read and ICP stores | authority / state | Tenant/person/conversation scope, selection revision, time window, and source receipt are exact and restart-safe. |
| Public web query selection | current web policies and transient comparison adapter | authority / privacy | A query crossing the network is bounded and explicit. Private file bodies/history cannot silently become query material. |
| Generic `web_research` | `ExecutionKernel._web_research` | effect / lifecycle | It persists accepted pages into Raw/Inbox and is therefore `mutate`; it is not a P2 read adapter. |
| Tool registry and execution | `ToolSpec`, `ExecutionKernel.execute` | authority / effect | Security ID, closed schema, risk, confirmation, effect fence, audit start, and uncertain post-start outcome are code-owned. |
| Source-derived effect recheck | `AgentRuntime._source_derived_effect_can_start` | authority / state | Mutable source identity and current capability permission are rechecked immediately before disclosure/effect entry. |
| Typed read outcome | `CapabilityOutcome` and route-specific outcomes | deterministic | Empty, partial, unavailable, denied, and failed are not silently promoted to complete. |
| Durable Work Item / fixed WorkGraph | `interaction_control_plane` | state / lifecycle | Exact plan/step identities, CAS transitions, retry budget, typed outcomes, restart, cancellation, and completion live outside prompt history. |
| Effect idempotency and reconciliation | request effect fence, approved actions, host/Obsidian receipts | effect / lifecycle | Once an effect owner starts, no alternate runtime replay occurs. Uncertain effects reconcile by observation. |
| Deterministic completion | completion gates and fixed journey evaluator | deterministic | All required evidence and coverage predicates must hold before synthesis can claim completion. |
| Primary synthesis | primary Qwen runtime | publication | The primary receives only admitted evidence envelopes and remains the ordinary final-answer author. |
| Final source/permission check | route-specific publication transaction | authority / state | Actor, source revision, capability, and accepted outcome are rechecked at the last durable boundary. |
| Atomic assistant publication | storage transaction and accepted receipts | publication / lifecycle | One assistant row and its accepted outcome/Work Item completion are committed once. A publication failure cannot replay an effect. |
| Interaction trace | `interaction_control_plane.turn_trace` | observability | Body-free HMAC identities and closed outcome fields describe the actual primary path. |
| Supervisor trace join | `supervisor_trace_join.py` | observability | A late shadow result joins the already committed ICP trace through a bounded operational event; assistant metadata is never rewritten. |
| Legacy compatibility branches | `AgentRuntime`, `OrchestrationRouter` | mixed | Remain available until an exact replacement journey has accepted shadow/canary/production evidence and rollback. |

## Failure classification

The following symptoms are semantic failures and may be improved by GPT-OSS:

- wrong multi-source task class;
- missed continuation when no exact pending state owns it;
- incomplete decomposition or evidence-domain selection;
- weak query intent or synthesis criteria;
- omission detected after deterministic evidence checks.

The following are not semantic failures and cannot be repaired by another model:

- stale/missing Work Item state, source revision, manifest, adapter, policy, or
  endpoint epoch;
- concurrent CAS loss, cancellation, deadline expiry, process restart, or an
  unavailable provider;
- late permission denial, cross-user source, private-data disclosure boundary,
  or unsupported effect class;
- malformed typed outcome, partial coverage, failed deterministic completion,
  publication rejection, or uncertain effect;
- compatibility-owner drift or an attempted second execution/publication owner.

## Frozen invariants

1. Exact deterministic lanes run before semantic planning.
2. A model object is data and never authority.
3. Only code resolves capability, adapter, security, effect, source, actor,
   confirmation, idempotency, deadline, and publication ownership.
4. ICP owns durable continuation, outcomes, completion, and restart behavior.
5. A turn has one runtime owner and one final publication owner.
6. No fallback or alternate runtime starts after an effect owner or durable
   promoted journey has begun.
7. Laptop absence preserves exactly one bounded primary path before ownership.
8. Source and authority are rechecked before use and before publication.
9. Empty, partial, unavailable, failed, and uncertain states remain honest.
10. Production observability retains no prompt, response, evidence body, raw
    identifier, private path, credential, or chain-of-thought.

## Baseline and promotion evidence

The synthetic fixture battery and isolated 8/8 endpoint protocol trial prove
contract/runtime behavior only.  They are not production baselines.

The promotion baseline is accepted only from joined body-free production
events whose primary side is an already committed `TurnTrace`.  At minimum it
must report, per approved task class:

- eligible, invoked, skipped, parsed, admitted, and rejected counts;
- actual primary route, capability outcomes, completion and publication class;
- planner latency bucket and laptop-off/saturation/timeout fallback class;
- partial/false-completion proxy counts, final authority recheck, restored state,
  and retry occurrence;
- duplicate capability/effect/publication counts (effects and publication must
  remain exactly zero/one as applicable).

No P2/P4/P5 promotion and no P6 heuristic deletion may cite an isolated or
synthetic report as this evidence.  Until the joined production window is
accepted, the replacement remains default-off/shadow and every legacy owner
and rollback path stays installed.
