# S4-R8F sole-facade cutover readiness

## Verdict

The measured branch has hidden the legacy dialogue adapters and replayed the
thirteen contours plus the historical ten-file union (1,197 passed). Scalar,
archived-source and message-topic remain parity; the other ten contours are
now `preserved` under the sole facade. Independent R8E review of
`f44c4e7c2f4a693bcaac91c4a9861fa6e8eef13b` is `accepted`, so formal
`cutover_ready` is true. Production remains `0.208.0` until a clean release
boundary.

This package changes the dialogue catalogue: `memory_search`, `source_search`
and `message_search` are no longer model-visible and are not executable in
dialogue scope. They remain internal (and mission, where previously declared)
adapters. Production `0.208.0` is unchanged until a clean release boundary.

## Evidence boundary

`cutover_readiness.py` builds one immutable canonical report from:

- the released R8C archive parity report;
- the closed R8D message-exact adapter binding;
- the mainline-integrated R8E memory-exact adapter binding; and
- a body-free manifest of the exact historical R8 failures.

The builder pins the live Friday source release, exact R8C seven-case manifest,
full R8C case/dimension measurement, fixed thirteen-contour case manifest, R8D
measurement head `2fa079eb4de1d33535798e24552f85db3b9ccfd2` and R8E accepted
head `f44c4e7c2f4a693bcaac91c4a9861fa6e8eef13b` with status
`accepted`. The report carries only a closed code-owned vocabulary,
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
| Current file | preserved | The authenticated V12 current-file contract remains separately owned and held after catalogue hide. |
| Message window | preserved | Queryless exact windows dispatch through `archive_search`. |
| Temporal `as_of`/`known_at` | preserved | The archive handler expresses `as_of`/`known_at` and derives the R8E snapshot. |
| Memory graph | preserved | `include_graph` runs the bounded R8E projection through the shared facade. |
| Follow-up | preserved | Follow-up guards held after catalogue hide. |
| V12 | preserved | The V12 archive reader remains a separate dispatch path and held after hide. |
| Restart | preserved | Exact cursors survive storage reopen; process rehydration stays fail-closed. |
| Fallback | preserved | Primary-only fallback held after catalogue hide. |
| Stale legacy call | preserved | Dialogue calls to the three legacy adapters fail closed; internal adapters remain executable. |
| Final reauthorization | preserved | One final publisher consumes exact receipts through the archive composite. |

R8E review status is `accepted`, so `cutover_ready` is true. The shared
archive handler expresses exact window, temporal and graph intents; legacy
dialogue adapters are hidden. Release and activation remain Mainline-owned.

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
   intents. Done.
2. Extend the single archive request/dispatch owner to express those intents
   while preserving one fresh authorization and one final publisher. Done.
3. Replay all thirteen contour guards and the exact historical ten-file union
   with legacy adapters hidden from dialogue and stale calls closed. Done:
   1,197 historical nodes passed; contour guards held.
4. Catalogue hide is in this package. Formal `cutover_ready` is true after
   independent R8E acceptance of `f44c4e7c2f4a693bcaac91c4a9861fa6e8eef13b`.
   Release and activation remain Mainline-owned.
