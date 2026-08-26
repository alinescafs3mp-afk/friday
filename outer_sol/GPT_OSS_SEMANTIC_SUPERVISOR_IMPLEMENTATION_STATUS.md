# GPT-OSS semantic supervisor implementation status

- Updated: 2026-08-26
- Architecture order:
  `outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_AND_POLICY_KERNEL_ARCHITECT_BRIEF.md`
- Phase: **P0 audit frozen, P1 contracts and shadow only**
- Base: `main` at `673b8715e8f8634fef02636523eafbe241375af0`
- Production identity is unchanged. This package does not bump Friday, does not
  replace `V12Planner`, and does not take router, tool, effect or publication
  ownership.
- Isolated non-owning work: the live golden-journey files, ICP stores, V12
  handlers and release identity files were not edited.

## P0: semantic guesses versus real invariants

Inspected at `673b871` without changing production ownership.

| Kind | Current owner | Evidence |
|---|---|---|
| Semantic heuristic | `V12Planner` prompt and `TurnPlan.route` | `friday/orchestration/planner.py` system prompt; `TurnPlan v1` is one exclusive route |
| Semantic heuristic | Route-specific planner wording | file/archive/web evidence rules in `TurnPlan._validate_relationships` |
| Deterministic invariant | One user-visible runtime owner | `OrchestrationRouter.__getattr__` delegates non-chat surfaces to `_legacy`; effectful plans stay legacy-owned |
| Deterministic invariant | Fail-closed router mode | `RouterMode.fail_closed`; `FRIDAY_ROUTER_MODE` unknown → `legacy` |
| Deterministic invariant | No double-effect replay | router refuses to retry a canary exception through legacy after an effect owner starts |
| Authority | Code-owned capability gate | `friday/permissions`; V12 handlers recheck source identity before publication |
| Authority | Model output is not permission | planner prompt: describe intent, do not authorize; `ToolIntent.effect` is still validated against route |
| Lifecycle / state | Pending durable Work Item admission | `friday/pending_durable_turn.py`; router asks the proven owner and retains legacy on uncertainty |
| Lifecycle / state | Exact ordinal / cancel | `archive_candidate_cancel_requested`, `parse_archive_candidate_ordinal` |
| Publication guard | Capability outcome gate | `require_complete_read_only_publication`; one message, citations, authority recheck |
| Legacy compatibility | Default runtime | `FRIDAY_ROUTER_MODE=legacy`; shadow V12 plans without a second answer |
| Optional secondary | Advisory extract / document map | `SecondaryBrainScheduler`; `PLAN_CANDIDATE` already exists as an advisory workload and is unused in product routing |

Frozen for this package:

- no second runtime owner;
- no second effect owner;
- laptop-off remains the unchanged primary path;
- GPT-OSS output remains data.

## P1: what landed

Closed contracts, a bounded code-owned manifest, a pure Policy Kernel and a
shadow observer. A proposal never becomes the turn owner.

| Surface | Role |
|---|---|
| `friday/orchestration/supervisor_contracts.py` | `CapabilityManifest`, `SupervisorInput`, `SupervisorProposal`, `SupervisorReview` |
| `friday/orchestration/capability_manifest.py` | Bounded projection from the current `TurnInput` |
| `friday/orchestration/policy_kernel.py` | Schema/manifest/effect/budget admission |
| `friday/orchestration/execution_plan.py` | `ValidatedExecutionPlan` sealed by the kernel; `parse` is forbidden |
| `friday/orchestration/semantic_supervisor.py` | Eligibility, secret-free input, shadow call |
| `friday/orchestration/supervisor_observation.py` | Body-free structural observation |
| `FRIDAY_SEMANTIC_SUPERVISOR_MODE` | `off\|shadow\|assist\|canary`, unknown → `off` |

P1 behaviour:

- default mode is `off`;
- empty task allowlist never invokes the supervisor;
- exact pending Work Item, `отмена`/`cancel` and standalone ordinals stay on the
  existing deterministic lane;
- small talk and ordinary dialogue stay on the primary path;
- `assist`/`canary` are accepted labels and still only shadow;
- `ValidatedExecutionPlan` cannot be built by parsing model JSON;
- effect class is taken from the capability catalog, not from `risk_hints`.

The live `OrchestrationRouter` is not wired. Shadow is invoked only through
`observe_semantic_supervisor_shadow` (tests today; a later non-owning hook can
reuse it without changing ownership).

## Next admitted step (not this commit)

P2 may promote one read-only journey, `compare_current_file_with_current_web`,
behind an independent flag, only after a shadow evaluation report on
representative turns. Effect planning remains out of scope.

## Isolation from concurrent Sol work

Not edited:

- `handoffs/Sol/`, `handoffs/SolGoodman/`
- `outer_sol/PROJECT_IMPLEMENTATION_STATUS.md`
- `outer_sol/OPTIONAL_SECONDARY_BRAIN*`
- `friday/interaction_control_plane/`
- V12 handlers (`file_read.py`, `archive_read.py`, selected-archive explanation)
- `CHANGELOG.md`, `friday/__init__.py`, `pyproject.toml`, `README.md`,
  `docs/OPERATIONS.md`, `docs/RELEASE_CHECKLIST.md`
- `friday/orchestration/router.py`
