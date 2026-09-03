# S4-R8F sole-facade cutover readiness

## Verdict

The measured branch is **not ready** to make `archive_search` the sole
dialogue-facing retrieval facade. Scalar memory, archived-source and
message-topic membership/order are now parity, but every exact message-window,
bitemporal-memory, graph, restart, fallback and final-publication path is
either contract-only or unmeasured at the shared runtime boundary.

This package is evidence only. It changes no runtime, tool catalogue, schema,
store, release metadata or production state. A `PACKAGE_READY` handoff means the
negative readiness result is reproducible; it does not authorize cutover.

## Evidence boundary

`cutover_readiness.py` builds one immutable canonical report from:

- the released R8C archive parity report;
- the closed R8D message-exact adapter binding;
- the mainline-integrated R8E memory-exact adapter binding; and
- a body-free manifest of the exact historical R8 failures.

The builder pins the live Friday source release, exact R8C seven-case manifest,
full R8C case/dimension measurement, fixed thirteen-contour case manifest, R8D
measurement head `2fa079eb4de1d33535798e24552f85db3b9ccfd2` and R8E integrated
head `f44c4e7c2f4a693bcaac91c4a9861fa6e8eef13b` with status
`integrated`. The report carries only a closed code-owned vocabulary,
canonical repository-local paths, counts and digests. It contains no query,
message, excerpt, prompt, tool output, actor/tenant/person/conversation ID,
private path or source body. Parsing rejects extra or duplicate keys,
non-finite numbers, forged/crossed bindings, path aliases, mutable collections
and any case outside the fixed manifest.

## Historical rejection matrix

Exact-release candidate `7848cc45ad8ddda3702b1aa560d1d42d5dea2acc`, based
on `9928e83d26061cc3df1198815ca9ac9f4481080f`, finished with 60 failures and
24,993 passes in 368.52 seconds. The sorted exact-node manifest is represented
only by digest
`e1d8d50860ad84ee3a117d48171af560f47a52750dd2d01ded1460ef792ef8d2`.

| Root cause | Failed nodes | Present interpretation |
|---|---:|---|
| Premature legacy-adapter retirement/stale treatment | 34 | R8A restored compatibility; R8B added internal scope; actual retirement still needs replay under activated exact lanes. |
| Over-strict classifier JSON handling | 16 | R8A restored released routing; a sole-facade replay must preserve the count and temporal/window routes. |
| Over-strict tool-turn admission | 5 | R8A restored released effect/control semantics; catalogue cutover must not change them. |
| Transport capability/refusal regression | 4 | R8A restored native/textual fallback behavior; the future catalogue union remains unmeasured. |
| Small-talk catalogue-selection refactor | 1 | R8A restored the no-tool boundary; it must remain invariant during retirement. |

The canonical report further binds each repository-file/root-class partition to
its own node-manifest digest using domain
`friday/s4-r8f-historical-failed-node-group/v1`; the eleven partition counts sum
to the exact aggregate manifest. It deliberately omits parameter values from
the historical node IDs. A bounded rerun at the rejected candidate reproduced
60 failures and 1,137 passes; the current measurement branch passed all 1,197
nodes. That proves compatibility restoration, not sole-facade readiness.

## Current contour matrix

| Contour | Status | Exact result |
|---|---|---|
| Scalar | parity | Current archive facade matches the measured legacy memory adapter. |
| Archived source | parity | Current archive facade matches the measured legacy source adapter. |
| Message topic | parity | Membership is 3/3 and candidate order is 3/3 after the measured topic-order repair. |
| Current file | unmeasured | The authenticated V12 current-file contract remains separately owned. |
| Message window | contract only | R8D has a queryless exact contract, but this branch does not wire it through `archive_search`. |
| Temporal `as_of`/`known_at` | contract only | R8E can express the exact snapshot; the model-facing archive request cannot. |
| Memory graph | contract only | R8E has a bounded graph projection; shared archive orchestration does not consume it. |
| Follow-up | unmeasured | Legacy follow-up state and archive cursors have not been replayed as one facade. |
| V12 | unmeasured | The V12 archive reader remains a separate dispatch path. |
| Restart | contract only | Exact cursors survive storage reopen, but runtime rehydration is not wired here. |
| Fallback | unmeasured | Primary-only fallback is preserved, but the future sole-facade catalogue union is not replayed. |
| Stale legacy call | unmeasured | Legacy adapters are intentionally still dialogue-visible, so post-cutover staleness cannot yet be asserted. |
| Final reauthorization | contract only | Exact lanes have late reauthorization contracts; one final publisher still does not consume all exact receipts through the shared facade. |

R8D/R8E contract presence is never promoted to runtime parity. R8E is
integrated on mainline with open-handle provenance, but the shared archive
request still cannot express exact window, temporal or graph intents.

## Minimal later shared-file set

After exact foundations are released, the union of files named by the remaining
shared-runtime obligations is:

- `friday/agent_runtime/__init__.py`
- `friday/agent_runtime/tool_protocol.py`
- `friday/execution_kernel/__init__.py`
- `friday/orchestration/capability_binding.py`
- `friday/retrieval/archive_search_contract.py`
- `friday/retrieval/archive_search_service.py`
- `friday/server.py`
- `friday/turn_intent_policy.py`

This is a measurement, not a write lease. Exact foundation files are excluded
from this set because their corrections and release precede shared activation.

Every contour also carries exact repository-local pytest selectors. R8F checks
that each selector resolves to a test definition; they are integration replay
references, not a claim that every referenced selector ran in this package's
bounded slot. The R8F suite and both historical ten-file unions are the executed
measurements recorded in its handoff.

## Cutover sequence

1. Keep the integrated R8E/R8D foundations unactivated in the dialogue catalogue
   until the shared archive request can express exact window, temporal and graph
   intents.
2. Extend the single archive request/dispatch owner to express those intents
   while preserving one fresh authorization and one final publisher.
3. Replay all thirteen contour guards and the exact historical ten-file union
   with legacy adapters hidden from dialogue and stale calls closed.
4. Require zero mismatches and no contract-only or unmeasured contour before
   changing the catalogue. Release and activation remain Mainline-owned.
