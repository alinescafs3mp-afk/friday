# Friday 1.0 acceptance contract

This is the single entry document for RC → 1.0 user acceptance. It does **not**
replace [`QUALITY_GATE_TIERS.md`](QUALITY_GATE_TIERS.md),
[`LIVE_BATTERY_RUNBOOK.md`](LIVE_BATTERY_RUNBOOK.md) or
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md). Live status and defects live only
in [`outer_sol/PROJECT_BACKLOG.md`](../outer_sol/PROJECT_BACKLOG.md).

**BATTERY_INCOMPLETE: Astra is repairing the harness before the first diagnostic baseline. Готовность батареи не означает, что Friday принята как 1.0.**

Machine matrix: [`tools/release_1_0_capability_matrix.json`](../tools/release_1_0_capability_matrix.json).
Wrapper: [`tools/release_1_0_acceptance.py`](../tools/release_1_0_acceptance.py).
Additional journeys: [`tools/release_1_0_live_journeys.py`](../tools/release_1_0_live_journeys.py).

## Scope proposed for Astra / owner freeze

| Class | Capabilities |
| --- | --- |
| Required 1.0 | Dialogue, files/ingestion, archive search, Inbox/KG, Admin UI, Telegram adapter and dedicated live round-trip, web/mixed, backup/restore, privacy, generated artifacts, CLI, primary-only when secondary is absent |
| Optional when enabled | Engineer Mode, Coding Mode, Host Capability Plane, secondary assist |
| Beta, not 1.0 | Obsidian core; physical Android remains unfinished |
| Out of scope | Companion plugin, Pandora P0H deletion, Gemini parity, off-machine mirror target, provider credential rotation |

A broken promised function cannot be relabeled beta to obtain a green table.
Owner-parked proofs stay `NOT_RUN` / `OUT_OF_SCOPE` and never count as PASS.

## Existing gates (unchanged, still mandatory)

1. `tools/quality_gate.py --tier exact-release` — closed inventory, no skips.
2. `tools/synthetic_live_acceptance.py --suite all` — 160 executions of sealed A/B cases (120 focused + 40 P06). Not 160 extra unique scenarios.
3. `tools/synthetic_live_battery.py --both` plus the authenticated B09 final binder —
   official 200+200; **B does not start if A is red**, and closed-only B cannot certify A+B.

New R10 cases are an additional identified set. Sealed A/B bytes are not rewritten.
The additional test functions and exact parameters are declared in
`tools/quality_gate_inventory.tsv`. Native namespace/bootstrap probes use
`exact-release` / `host-tool`; model-free protocol and orchestration tests use
`change` / `unit`. They do not replace exact-release or the 160/A+B live gates.

## Layers

| Layer | What it proves | What it is not |
| --- | --- | --- |
| Deterministic | Unit/integration/UI/security and R10 isolated TestClient journeys | Live-model quality |
| Isolated live | Real candidate, real DB/indexes/workers, local LLM endpoints | Production home, live Telegram singleton |
| User UI | Playwright Chromium Admin UI | Additional browser support awaits the owner scope freeze |
| Deployment/device | Native install/restore, dedicated test bot, physical Android | Working owner phone or live bot |

## GO / NO-GO / INCOMPLETE

Proposal (do not weaken any stricter existing rule):

- **GO** only as a conjunction: every required capability listed, every required case executed in the required layer, no `SKIP`/`BLOCKED`/`NOT_RUN`/`MANUAL_PENDING` in the required set, no open critical/high defects, exact-release + 160 + clean A+B + additional R10 live, sealed candidate/wheel/runtime identities.
- Any tenant leak, unauthorized effect, silent data loss, false completion or duplicate irreversible action is **NO-GO**.
- Missing proof is **INCOMPLETE**, which also forbids declaring 1.0.

This wrapper **never prints GO**. Astra records the verdict in the single backlog after the checkpoint.

## Exact commands

Model-free harness (safe to run anytime; does not occupy the full-gate slot):

```bash
umask 077
r10_collection_dir="$(mktemp -d -p /var/tmp friday-r10-collection.XXXXXXXX)"
.venv/bin/python -I -B tools/quality_gate.py --inventory-collection "$r10_collection_dir/nodes.json"
.venv/bin/python -I -B tools/release_1_0_acceptance.py --audit-only --collection "$r10_collection_dir/nodes.json"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --preflight
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --negative-control
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py --audit-only
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py --suite all --audit-only
.venv/bin/python -I -B -m pytest -q tests/test_release_1_0_acceptance.py tests/test_release_1_0_journeys.py tests/test_release_1_0_fault_controls.py tests/test_release_1_0_deterministic.py tests/test_release_1_0_coverage.py tests/test_release_1_0_live_cases.py tests/test_release_1_0_native.py tests/test_release_1_0_native_process.py tests/test_release_1_0_remaining_oracles.py tests/test_release_1_0_graph_oracles.py tests/test_release_1_0_secondary_relay.py tests/test_release_1_0_surface_discovery.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --plan diagnostic-baseline
```

The audit requires the canonical retained collection. It verifies each executable
case against the actual dispatch callable, source path, layer and exact test node;
an unrelated existing test does not satisfy the binding. Surface links must be
explicit and satisfy every required layer. Matrix scope, obligations and links
must agree. Collection proves test membership, not execution or acceptance.
The CLI audits the union of source routes, OpenAPI and registered runtime
entrances, including hidden routes, nested router prefixes and ASGI mounts.
Runtime registration is inspected in a separate bounded process using the
canonical private home and empty env file; ambient provider settings are
excluded and network calls are denied. Application lifespan is not started.
A discovery failure is red and cannot fall back to source-only coverage.
This describes default route construction, not deployed or enabled-config
acceptance; those configurations still require their own evidence.
Collection never credits isolated-live, user-ui or deployment-device execution;
those layers require validated execution receipts. The audit consumes canonical
exact-gate receipts for deterministic and registered canonical-pytest user-ui
cases. Every UI member must be an observed browser node in the validated UI phase;
mixed or nonbrowser membership leaves that case uncredited. Native/device receipt
ingestion remains open. Missing live proof and unbound surfaces keep the audit red. The three generic
comparator controls have the `harness` layer and do not cover product surfaces;
scenario fault injection is exercised separately by `test_release_1_0_fault_controls.py`.

The named-source Word handler in `tools/release_1_0_live_cases.py` freezes three
independent first-turn requests. It submits the TXT in the same signed chat call,
then verifies the owned source bytes, exactly one downloaded DOCX, descriptor and
inline byte agreement, assistant-history binding and independently parsed source
facts. Its protocol tests retain real HTTP ingestion/persistence/download with a
synthetic model result. They are model-free harness evidence: collecting or
executing these protocol tests does not execute the live case.

`tools/release_1_0_native.py` now dispatches the three Word cases through the
canonical candidate wheel build, private source snapshot, local UDS relays,
network/filesystem namespaces, import-authority checks and process cleanup.
Each case has a new private home, a sealed request, the matrix deadline, bounded
worker output, observed primary HTTP activity and exactly one submission. The
controller checks retained source/output bytes against the receipt and preserves
the wheel and private evidence after deleting only a positively reaped case home.
Setup failures and interruptions have one root with a complete selected-case
report; all required cases are also listed, with unexecuted cases `NOT_RUN`.

The implemented configuration is explicit **primary-only**. An enabled secondary
is rejected as `native_secondary_transport_not_implemented`; it is never silently
disabled. Registry `executable=true` means the live dispatch exists, not that it
has passed a real model run or independent acceptance. Full instrument review,
coverage completion and the diagnostic baseline remain outstanding.

The shared relay now has an explicit four-endpoint opt-in for secondary; its
canonical default still requires the three original endpoints. Independent
model-free tests route four HTTP services from a separate guarded process and
exercise failed listener/thread startup cleanup. This transport seam does not
yet enable secondary execution in the native case controller.

Diagnostic baseline / final (exclusive native/UI/model-heavy slot; Astra coordinates).
Do not overlap another quality gate, Mainline gate or Playwright run. Do not use
the production home or the live Telegram singleton.

```bash
umask 077
export FRIDAY_SYNCTHING_AMD64_TARBALL="${FRIDAY_SYNCTHING_AMD64_TARBALL:-$HOME/.cache/friday/test-assets/syncthing-linux-amd64-v2.1.3.tar.gz}"
candidate_sha="$(git rev-parse --verify 'HEAD^{commit}')"
base_sha="$(git rev-parse --verify "${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}")"
GOLDEN_JOURNEY_RELEASE_ROOT="$(readlink -f -- "$HOME/.jericho/wheel-only-releases/8b6a8c13ce54b8b07192cb6f5b820953da4efcb5")" || exit 1
test -d "$GOLDEN_JOURNEY_RELEASE_ROOT" || exit 1
export GOLDEN_JOURNEY_RELEASE_ROOT
GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT="$(readlink -f -- "$HOME/.jericho/runtime/release-tools-020800/production-observation-private-020800-75b165a2.json")" || exit 1
GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT_SHA256="207aad4458d048c0abd9c52befd05b7b29bda7988e8c62fa62fb3a5b6eac7f66"
test -f "$GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT" || exit 1
export GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT
export GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT_SHA256
evidence_dir="$(mktemp -d -p /var/tmp friday-exact-evidence.XXXXXXXX)"
.venv/bin/python -I -B tools/quality_gate.py \
  --tier exact-release --candidate-sha "$candidate_sha" --base-sha "$base_sha" \
  --evidence-dir "$evidence_dir"
```

Then, on the same frozen candidate, with a deployed `FRIDAY_ENV_FILE` of mode `0600`
that is **not** the production home, first create `review_root`, the root key and the
preregistered plan using
[`LIVE_B09_CONTENT_REVIEW.md`](LIVE_B09_CONTENT_REVIEW.md), then run:

```bash
test -n "${FRIDAY_ENV_FILE:-}"
test -f "$FRIDAY_ENV_FILE" && test ! -L "$FRIDAY_ENV_FILE"
test "$(stat -c %a -- "$FRIDAY_ENV_FILE")" = 600
umask 077
if PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py \
  --env-file "$FRIDAY_ENV_FILE" --both --concurrency 4 \
  --run-directory "$pair_root" \
  --b09-review-plan "$review_root/plan.json" \
  --root-review-key /secure/operator/path/friday-b09-root.key; then
  false
else
  r10_b09_closed_rc=$?
  test "$r10_b09_closed_rc" = 4
fi
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_b09_evidence.py task \
  --key-file /secure/operator/path/friday-b09-root.key \
  --plan "$review_root/plan.json" \
  --pair-report "$pair_root/pair-aggregate.json" \
  --b03-evidence "$pair_root/battery-b/pass-03/evidence/raw-responses.jsonl" \
  --b09-evidence "$pair_root/battery-b/pass-09/evidence/raw-responses.jsonl" \
  --output "$review_root/review-task.json"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py \
  --env-file "$FRIDAY_ENV_FILE" --suite all --concurrency 4
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_live_journeys.py \
  --env-file "$FRIDAY_ENV_FILE" --candidate-sha "$candidate_sha" \
  --evidence-dir "$evidence_dir/r10-native" --run-live
```

Create the plan before the run. The TASK command refuses a red closed pair. Deliver
that sealed TASK to the independent visible lab reviewer, authenticate its actual
RESULT, and verify the signed final pair using
[`LIVE_B09_CONTENT_REVIEW.md`](LIVE_B09_CONTENT_REVIEW.md). The initial A+B command
returns `4` while content evidence is pending. Neither `pair-aggregate.json` nor a
closed-green B report is a release acceptance artifact by itself.

A red A still finishes all ten A passes and must not start B. Additional R10 live
may run after a red required set only when isolation/safety preconditions hold;
that does not make the aggregate certifying.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Selected wrapper action green; never aggregate release GO |
| 2 | Matrix/sealed-audit/preflight failure |
| 4 | Live run red, retained setup-root failure, or closed-only A+B awaiting plan-bound content receipts |
| 5 | Additional live `NOT_RUN` before an owned run directory exists |
| 130 / 143 | Additional live controller interrupted by INT / TERM |
| 70 | Live-acceptance teardown hard exit |

Canonical quality-gate and live-battery codes are unchanged. The wrapper cannot
override them.

## Evidence

Private traces stay under `data/live-battery-runs/` (`0700`/`0600`) or the
caller-supplied evidence directory. Public summaries may contain counts, IDs,
closed failure codes, hashes and privacy verdicts only. Raw questions, answers,
worker logs and secrets do not enter Git, docs or chat.

Per-case fields: run/case/capability IDs, suite/manifest/candidate identities,
layer, status, expected/observed, closed failure codes, root-cause class
(`product` / `harness` / `transport` / `environment`), timings, opaque proof
refs, cleanup, retries.

## Manual only where a safe contour does not exist

- Live Telegram round-trip: dedicated test bot, never the production singleton.
- Physical Android Obsidian: owner-parked; remains beta.
- Soak duration/load: freeze before the final run; a short smoke is not a soak.

## Known product gap this battery must not hide

First-turn Word/DOCX generation on live `0.208.57` fails when the source `.txt`
filename is spoken (`WORD003_MAKE_FILE_PRE_RENDER_REJECT_SOURCE_TXT_STEM`).
Case `R10-LIVE-DOC-WORD-FIRST-GEN` stays required with expected PASS. The first diagnostic baseline must record the actual observed outcome on its
frozen candidate; the historical defect does not preassign FAIL.

### Native harness checkpoint008 (model-free, not live acceptance)

Native Python starts with `-I -S -B`; dependency directories are added without
`.pth`/sitecustomize processing, and the tools namespace is bound to the frozen
snapshot. Reader/unknown lifecycle failures fence further dispatch and preserve
the private home. Fully audited cleanup may clear after explicit TERM/KILL or
a timeout; those events still fail the affected case. The retained wheel is
validated again after dispatch. HTTP accounting covers constructor, startup,
case and shutdown under the active probe/guard. Tests use synthetic transports.
Word requires the active canonical owner projection and independent durable
binding of the signed Telegram principal; wrong actors fail before submission.

Checkpoint014 adds controller-bound runtime/model identity. A bounded model-free
probe loads the installed candidate settings under the same complete worker
environment before live relays exist. Its retained receipt seals the live request.
The worker recomputes environment/settings identity before execution, on the same
settings object after shutdown, and after a fresh settings load; all must match.
One phase-aware guard and HTTP probe span product imports through shutdown, with
cold phases denied, seccomp before product import and thread census/relay cleanup
checks. Guards stay installed through process exit. Probe and live work share the
case deadline; sealed request reads reject replaced, symlink and nonregular files.
Setup tracebacks are retained only in a bounded private probe log.

Checkpoint018 sets `OMP_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1` in the
private worker environment before imports, overriding inherited pool sizes.
The full environment digest binds these limits; the native census is unchanged.
An actual sandbox probe followed by `worker_main` verifies app startup, shutdown,
runtime identity and clean containment teardown. Its diagnostic handler makes no
model calls, so the worker correctly reports `native_primary_http_not_observed`,
never live PASS. A deliberately surviving thread still fails both census and
containment cleanup; the outer lifecycle reaps the entire child process group.

The sandbox settings test uses its own private package projection instead of
binding the checkout as an installed site; source directory permissions cannot
supply that authority. These are model-free instrument checks. Independent review
of the complete native repair remains pending. Enabled secondary and the complete
required live/UI/device receipt/coverage layer remain OPEN; baseline/final NOT_RUN.

### Reading canonical deterministic execution evidence

The structural `--audit-only` command can additionally consume a private
`quality-gate-summary.json` without executing tests again:

```bash
.venv/bin/python -I -B tools/release_1_0_acceptance.py --audit-only \
  --collection "$r10_collection_dir/nodes.json" \
  --gate-receipt "$evidence_dir/quality-gate-summary.json" \
  --gate-receipt-sha256 "$exact_release_receipt_sha256" \
  --candidate-sha "$candidate_sha" --base-sha "$base_sha" \
  --wheel-sha256 "$candidate_wheel_sha256"
```

Supply the receipt and wheel digests from the separately retained frozen run
identity. The reader checks candidate/base/tree, the current inventory and exact
parameter partition, the installed test wheel, the complete executed node union,
deadlines, no retries and no comparison/measurement substitution. The candidate
source must remain frozen through the read. Every tracked file is hashed against
its actual Git blob through a stable descriptor, independently of cached index
metadata; hidden index flags, extra ignored files, symlinks and hardlinks are
refused. Read receipts in a clean isolated candidate checkout. Receipt bytes are bounded, private,
single-link, canonical JSON and read through one stable descriptor.

The reader also validates the canonical sequence of static/build/install/
collection/execution steps, exact requested worker counts and effective workers
derived from selected modules, both scratch-group projections and workload
metrics, and the recorded host capacity/package/AppArmor contour. Recorded host
evidence is bound by the externally retained receipt digest; reading it does not
run host probes again. Scratch observations retain the canonical writer's
`enforced=false` meaning and do not become an enforced disk quota. Browser tests
and nightly-only nodes cannot increase the deterministic case credit.

`gate_execution` describes accepted deterministic and registered browser evidence. The report also
contains every required case with its actual layer status and separate
`surface_execution_gaps`; without a receipt those cases remain NOT_RUN. Native
Word protocol tests never become live evidence through an exact-gate receipt.
`valid` retains its explicitly stated structural scope; `execution_complete` and
`go_emitted=false` prevent interpreting it as release acceptance. Native/device
receipt ingestion and remaining exact surface contracts remain unfinished.

Every case declares an `execution_driver`. Additional journey handlers, native
workers and harness controls keep their own dispatchers. `canonical-pytest`
cases register exact existing node IDs and surfaces in `PYTEST_CASE_BINDINGS`;
they execute only through `tools/quality_gate.py:execute_tier`. The audit checks
the actual gate callable, candidate test definitions (without importing test
modules), exact canonical collection membership, nodes, surfaces and layer.
Missing or mismatched registrations are errors. A journey-only run keeps these
cases explicitly NOT_RUN and reports INCOMPLETE; it never executes a second
pytest controller. A frozen exact-gate receipt can credit their actual layer
under the receipt rules for deterministic and registered browser cases. UI credit
retains the case's `user-ui` layer, exact membership and case duration ceiling;
the same candidate/wheel/inventory, topology, observed phase/deadline and owned
cleanup checks apply. The required denominator and structural classification
without receipts remain unchanged. This reader support is not an executed UI run.

Each executable case declares a positive integer `timeout_s` (at most 86400);
zero is allowed only for an unimplemented case. The reader validates this for
both loaded matrices and in-memory callers. It sums the recorded canonical node
durations for each credited deterministic case and rejects the entire receipt
when the sum exceeds that case's limit. `case_durations` names the measurement
basis, nanoseconds and limit; equality with the limit is allowed. Parallel nodes
still count towards this conservative work budget, not a reconstructed wall clock.

The receipt check validates completed evidence. The exact-release parent's
active START/FINISH ledger additionally interrupts node/case overruns while they
run, including setup and teardown, as described in the ownership section below.
The whole-stage controller timeout remains an independent outer bound.
The fifteen additional journeys now run in separate children via the existing
document-battery process lifecycle. Each gets a new private home and environment,
a sealed full-settings request and source/package digests. An exhausted case
budget interrupts the child; the final receipt also rejects a total duration
over budget, including setup and cleanup. SIGINT/SIGTERM unwind cleanup and retain
an interrupted receipt before propagating. Process exit and process-group audit
must prove cleanup before runtime paths are removed or the next case starts.
Uncertain cleanup retains every worker path and fences subsequent dispatch.
Case roots live outside pytest/gate temporary trees in private `/var/tmp`
directories. JUnit properties retain each receipt path and SHA-256. Before removing
JUnit and the installed projection, canonical exact-release now verifies each
child receipt against its selected node, first attempt, actual case deadline,
observed execution, successful cleanup and a fresh gate context. That context
binds base/candidate/tree/wheel/inventory identities, Python and actual installed
package and suite digests; it is sealed into the child's request and receipt.
Only the registered journey test passes this context to its child; harness unit
tests remain explicit diagnostics even when running inside the same gate.

The private summary's `r10_deterministic` records retain receipt paths and hashes,
request/settings hashes and observed timings. The acceptance reader reopens the
private canonical receipts and sealed full-settings requests and rejects
missing/replaced/public/nonregular files,
changed bytes/context, retries, missing/duplicate/wrong-case evidence and child
time exceeding either its case budget or the enclosing node duration. No native,
UI or device execution credit follows from these records. Private evidence remains
after normal cleanup; attempted and executed counts
are separate, and delegated canonical pytest cases stay NOT_RUN.

These model-free children deny network access and process execution before product
imports. They do not claim native OS filesystem isolation. Their local receipts
require the enclosing canonical gate's candidate/tree/wheel provenance;
standalone diagnostics cannot establish release acceptance. The child lifecycle
does not interrupt blocking controller setup itself. Aggregate interruption for
the multi-node canonical pytest cases remains open, as does independent review
of this driver. No existing node policy or case budget was increased.

The first two such bindings cover admin file pages and account lists: existing
HTTP privacy/owner/overseer/audit and event-loop assertions are combined with
new anonymous/user refusals, complete nonduplicated pagination, empty pages and
invalid bounds. These API cases do not establish browser or live model coverage.

Six graph/inbox bindings retain ten exact existing nodes for ten API surfaces:
current/historical neighbourhoods and direct entity/profile projections, temporal
and bounded admin overview, idempotent relation creation/audit, bulk candidate
review with persisted graph effects, inbox grouping/dismissal with no promotion,
and signed assistant-to-inbox with persisted pending identity. Checkpoint019 adds
ten supplemental HTTP/storage nodes: exact temporal topology and unique members,
response-to-persisted relation tuples and replay revisions/audit, immutable bulk
decisions and a state-preserving 201-ID refusal, a 204-item directory fixture with
explicit 200-of-201 truncation, and literal signed tenant/replay row identity.
Private fields and canaries are checked against bounded public projections.
Observed-result controls reuse the real scenario assertions. Nineteen private
product-path mutations that exposed former weak oracles now fail those tests;
the private replay does not alter product source. These checks strengthen the
instrument. Execution credit still requires the complete canonical gate receipt.
Owner-only graph tests do not establish cross-tenant
authorization, browser interaction or model behaviour. Simple route reachability
and the broad maturity smoke are not used to credit other surfaces.

The J08 deterministic recovery journey also exercises the admin backup list,
creation and download contracts. It checks the new list item/count, attachment
headers, manifest size/digest and the actual downloaded SQLite integrity/source
row. Anonymous and ordinary-user attempts must be refused without creating a
backup; missing files and an owned escaping symlink must return 404. The existing
isolated installation's audit key is frozen before creating the backup; an
independent HMAC calculation binds both new audit entries to that exact handle.
The key is never emitted. Decoded before/after projections must not contain the
backup filename. The existing stopped-snapshot restore continues in a separate
runtime. Nineteen injected boundary faults,
including another valid SQLite database with a matching forged manifest, must
turn these observations red; this includes a matching unrelated audit reference
and filename leaks in targets or JSON payloads. These are deterministic instrument checks; live
baseline and release acceptance remain NOT_RUN.

The experimental `quality_gate_process` helper preserves the gate parent's
host namespaces while adopting orphaned descendants with Linux subreaper
semantics. It signals only pidfd handles verified as direct children and requires
kernel ECHILD before restoring its prior state. Its parent loop also reaps exited
adopted children during execution, because deferred reaping breaks nested sandbox
process-group checks. A successful leader that leaves live descendants is not a
clean exit. Uncertain cleanup must retain scratch evidence and fence later phases.
The caller must own all process creation and defer cancellation across launch;
catchable controller signals must enter its cleanup path. SIGKILL of the gate
parent is not covered by this helper.

Eleven real process probes cover clean completion, mid-phase orphan reaping,
timeout, controller cancellation, errors, nested setsid/thread descendants,
non-child pidfd refusal, admission failure and uncertain cleanup. Their outer PID
namespace is only a test guard, and these nodes require the exact-release
host-tool tier. Wrapping a whole canonical phase in that namespace changes the
host's file-ownership view and is incompatible with exact-host admission.
The exact-release canonical parent now uses this ownership helper for the
static/build/install/collection commands and both pytest phases, as well as
initial admission and projection. Catchable SIGINT/SIGTERM are recorded through launch, then cause
owned cleanup; children inherit no blocked cancellation mask. Every nested
runtime home and the outer scratch root share an irreversible cleanup fence.
An uncertain cleanup retains those directories and denies later commands.
Ownership now starts before the first Git admission read and candidate clone.
Three fixed, bounded source files load into a private bootstrap namespace from
actual source bytes, without a product sys.path entry or bytecode-cache authority.
The owned Git admission verifies those loaded bytes against the requested commit
before projection. The projected candidate tools namespace retains its strict
source authority. Git, inventory Git reads and host prerequisites use the same
owner and bounded captured output; no unowned subprocess fallback is allowed in
this exact-release path. Catchable cancellation during admission or clone reaps
owned descendants before deleting scratch. Uncertain cleanup retains a partial
clone. Auxiliary early/late command proofs are mandatory in the v2 receipt;
missing/duplicate clones, foreign commands, unsuccessful cleanup and overlap with
measured phases prevent acceptance. Ten guarded real-Git/bootstrap probes and ten
additional reader mutations exercise these boundaries. Independent review of
this extension remains required before instrument acceptance.
These focused checks confer no canonical or live acceptance credit.

Exact-release summary v2 requires the sealed active-deadline evidence and owned
command cleanup records. Worker hooks emit bounded atomic FIFO START/FINISH
records around the entire runtest protocol, including setup and teardown. The
same parent drains events, reaps adopted zombies, and enforces unchanged per-node
limits, each case's summed concurrent intervals and the controller's existing
3600-second ceiling. Shared nodes execute once and charge every declaring case;
completed totals carry across non-UI/UI phases without charging idle time.
Equality passes; the first forbidden nanosecond expires. Worker restart is zero.
The canonical 20/4 worker topology and load/loadscope distribution are unchanged.

First-attempt counts come from observed events. Ordered event indices allow the
reader to replay the exact event stream, verify its digest and recompute all
case totals; missing/duplicate/foreign/malformed records cannot yield credit.
JUnit remains independently checked, with only its millisecond serialization
rounding allowed when compared to full protocol intervals. The v2 reader also
requires a successful exclusive cleanup proof for every measured command;
legacy v1 receipts cannot satisfy this revised instrument. Failed owned execution
writes a private non-certifying failure record when the evidence directory is
available. Parent SIGKILL remains outside this ownership guarantee.

Eleven actual pytest/parent probes cover two-phase shared membership, parallel
aggregate expiry, setup/teardown hangs, lost/duplicate events, worker crash,
catchable cancellation including the launch boundary, controller collection hang,
and an actual denied-kill cleanup that retains scratch. Twelve receipt mutations
exercise event replay and cleanup rejection. The instrument remains incomplete
pending independent review and the remaining coverage/scope work.

Eight administrative conversation cases now bind exact HTTP entry points to
model-free tests: chat feed/cursor/person transcript/reply queue, conversation
listing/transcript/archive/delete. Synthetic fixtures freeze people, conversation
and message membership before requests. Checks cover exact pagination and totals,
message projections, file authorship, cross-tenant audit, archive visibility,
retained history and cleared channel ownership. Reply tests inspect two distinct
pending queue records for two intentional identical requests; no sender or model
runs. Anonymous/ordinary-token access and missing or invalid inputs are refused.

Fourteen negative controls corrupt actual HTTP results or suppress/misdirect the
real persistence/audit operation. The same scenario assertions must fail at the
specific affected boundary. Audit expectations follow the private schema: raw
chat/actor strings and some integer state fields are deliberately absent, so
the tests independently verify persisted state and the bounded audit projection.
These source bindings await independent review and canonical execution. They do
not establish Telegram delivery, browser behaviour or retirement of active work
items.

Nine self-service conversation entrances also have source bindings under shared
archive mode. Exact own/foreign membership, history windows, downloadable export
bytes, search filters, reset state and latest-answer diagnostics are observed.
Rename/archive/delete preserve personal history and refuse foreign references.
The signed `current` route resolves the literal bridge person and channel through
history, rename, export, archive, explicit-ID unarchive and delete. API tokens and
another bridge person cannot use that reference. These are signed API requests,
not Telegram delivery or browser evidence.

The private fixtures share the administrative suite's deterministic setup without
changing its source. Thirteen negative controls alter actual returned membership,
bytes, diagnostic data or persisted operations, including omitted unarchive and
wrong-person reset. Export checks concern the caller's transcript; they do not
claim redaction of the caller's own supplied text. The earlier self-service
instrument has scoped independent acceptance; the strengthened reset oracle
awaits review. Canonical execution and live delivery remain unverified.

The self-service reset oracle now seeds another channel ID and another channel
kind for the same person, alongside the matching foreign-person channel. Both
first reset and repeat preserve every exact non-target session row, conversation
row and message. Three actual storage-write controls independently remove one of
user_id/channel/channel_id from DELETE reached through authenticated HTTP; each
must fail the unchanged persistence assertion. These are model-free oracle checks,
not Telegram delivery or canonical execution evidence.

Three token-management HTTP entrances now have exact deterministic bindings.
Listing checks literal membership/order/filter/revocation and safe metadata;
minting checks the stored hash, intended account, exact TTL, audit and real auth;
revocation checks formerly valid auth stops while both same-person and foreign
siblings remain usable. Ordinary/anonymous and delegated-owner refusals preserve
token target state (routine auth last_used_at touches excluded). Nine negative
controls alter real storage or HTTP effects, including a claimed refusal that
still creates a credential. Existing delegated-authority and TTL checks are reused.
A fractional TTL (1.5 seconds) currently returns200 and creates a token; its
expected400/no-write oracle remains a plain failure, with no skip or product fix.
This focused finding precedes the first full diagnostic baseline and does not
replace it; independent instrument review and canonical/live execution remain pending.

Reminder self-service now has exact GET/dismiss and Telegram-command bindings.
Actual signed HTTP and SQLite verify personal list membership/time/limits, durable
cancellation and rescans. Production bridge command/callback paths run through
controlled transports: visible bodies, working action IDs, selective keyboard
retirement and refusal boundaries are observed. Long-ID coverage keeps the body
visible and the valid sibling actionable while enforcing64-byte offered callbacks.
Eight real corrupted output/persistence controls must fail their specific oracle.
The delivery-chat migration case has no covered_surfaces: it is storage robustness
for a separately allowlisted destination, not an identity-binding entrance proof.
Astra reproduced29PASS7plainFAIL across28new and8existing nodes. Current product
source and the existing after-delivery test supersede G19's original pending-only
cancellation contract; old sent queue-ID buttons remain a supported compatibility
expectation, not a reason to restore the obsolete fifteen-second cancellation window.
The seven failing scenarios retain expectedPASS and no skips or product repairs;
this focused diagnostic is separate from FIRST full baseline. No live Telegram
consumer, external transport or production state was exercised.

User create/PATCH and the actual GET deletion/DELETE entrances now have exact
bindings. New HTTP checks compare persisted account fields, metadata/upsert,
attributed audit and unaffected siblings. Disable/reactivate changes a previously
working token's actual authentication and assigned role's HTTP permission. Ten
real output/persistence controls include omitted audit before the append-only
writer and a real write after a403; no audit trigger is removed. Ordinary account
creation and its controls use an explicit local origin. A separate required test
retains the plain defect: requested source=admin becomes local when role assignment
calls ensure_user with its default origin. It is not waived or fixed before FIRST
baseline. Initial instrumentation failure attempting to delete immutable audit
rows was corrected to omit the actual writer call; it supplies no negative credit.
Deletion positives are explicitly test-only enabled, private model-free fixtures.
Default production configuration remains code-owned disabled with503. Direct
quiescence=False and worker-off HTTP/gate=True are different prerequisites; their
readiness difference is not labelled a defect. Exact GET/DELETE authorization
refusals, account/tombstone preservation and a mutation after403 are observed.
The old confirmation test's synthetic direct-storage half does not certify HTTP
maintenance or live deletion. Nine existing deletion nodes are retained unchanged.
These focused results are not the frozen FIRST full baseline or final acceptance.

Token revision029 repairs Sol's independently reproduced false-green gaps. Mint
compares complete existing token rows, including last_used_at. Other auth paths
normalize only the exact authenticated credential's timestamp and independently
bound it to that request at storage's second precision; every other field/row
remains exact. Revocation now compares raw target rows, preserving the hash and
all fields except revoked_at. Complete revoke audit history across every target ID
must gain exactly one proper record, and repeat/missing404 must add none.
Delegated-admin GET now exercises the full safe metadata/filter/revoked contract,
including its own token. Expectations come from pre-request rows, with only that
validated auth timestamp substituted. Seven added real controls catch collateral
mint/refusal timestamps, extra missing-ID audit, delegated hash leakage, a forged
future auth touch and real lookup accepting a persisted revoked credential.
All14 old token nodes remain;22new+12oldAPI-token nodes33PASS1plainFAIL23.41s.
The fractional-TTL product failure is retained. Initial raw/redacted target-row
comparison was a harness error, repaired without dropping hash preservation.
Independent instrument re-review is pending; no fullbaseline or live credit.

Account revision030 binds five actual HTTP entrances: resolve, activity, preset,
supervisor and permission override. Literal people, ambiguity, safe match fields,
per-person audit, exact state preservation and subsequent real HTTP authority are
observed. Allow/deny/inherit preserve a separate override on the same person;
supervisor set/clear changes real hierarchy access and refuses cycles/self/missing
supervisors. Shared-archive activity distinguishes author from tenant and excludes
foreign, unattributed and out-of-window rows while preserving exact pagination and
full/redacted boundaries. Four existing nonshared/analysis HTTP tests are reused.
Thirteen real response/storage/writer controls must fail their specific oracles.
Positive authority fixtures have explicit local origins; two separate required
plain failures retain source=admin being reset tolocal on preset/override writes,
the same ensure_user origin root already exposed by user028. No product repair.
Initial test-only errors (matched_on is matching text, RawObject metadata_json,
and an uncommitted fixture UPDATE locking HTTP access) were corrected without
weakening expectations. Private source and raw activity data remain exact.
Token029 independent Grok review is scoped accepted after hash/node verification:
33PASS1retained fractionalTTLFAIL and two independent actual escaped-fault probes.
This is instrument evidence only; full instrument, FIRSTbaseline and final remain
incomplete. Identity HTTP is separately assigned to Sol, without duplicate work.

Revision030 final focused packet:21new+4oldHTTP =23PASS2plain source-originFAIL;
all13actualcontrols PASS. Audit before/after values are checked at the real route's
writer input using a pass-through recorder; persisted sanitized audit separately
retains exact history/count/person attribution and excludes private note content.
Comparing raw account values directly with the privacy-projected stored audit was
an initial harness mistake, now corrected. No private values are required in the
stored audit. Metadata packet169PASS. The old injected effective_non_ui_workers=20
became valid when the 20th nonUI module was added; the actual receipt mutation now
injects21, above canonical20. Exactly this prior parameter's digest is updated;
canonical worker policy, every prior semantic inventory rule and total node count
remain unchanged. No gate is softened and no product source is changed.

Revision031 adds Sol's exact identity GET/POST/object DELETE HTTP oracles after
Astra's full source read and independent24new+9old replay:30PASS3plainFAIL.
Three observed candidate defects remain required: owner-person filter audit turns
into shared-archive wildcard, delegated linked_by uses tenant rather than person,
and delegated admin can rebind an already owner-bound identity to another account.
The last observation is a failure of owner identity protection, not evidence that
the admin gained access to owner data. No product logic is repaired before FIRST.
Astra independently injected promotion of an unrelated account after a real403:
the initial refusal oracle stayed green. The private passing probe demonstrates
that instrumentation gap. Revised oracles compare all account/override rows on
reads, changes and refusals; three new actual account/override controls catch it.
Actual signed bridge authentication adds the literal chat_id/language metadata
and touches activity times; expected new values and preserved original metadata
are checked explicitly, rather than dropping metadata from preservation.
There are27newnodes and18actualcontrols. Product failures are plain/unskipped;
Sol's FIRSTbaseline label refers only to the focused first attempt, never the
FIRST full diagnostic release baseline. Further independent repair review pending.

Revision032 adds model-free HTTP personal-instructions/derived-profile oracles
and positive own-account GET coverage, preserving the existing forged-token case.
Nineteen nodes include nine actual observation/persistence fault controls.
Personal preferences are checked separately from the intentionally shared corpus:
PATCH writes one person's metadata, while profile reads the authorized corpus.
Exact rows, visible facts, guest access, capability refusal, clear/repeat and
bounded optional synthesis/fallback are observed. A controlled provider is not
live model quality evidence. Only own account updated_at may change on PATCH,
bounded by request time at the storage clock's whole-second precision.
HTTP-to-runtime-prompt inspection retains a plain shared-archive defect: the
saved personal preference does not reach that person's untrusted prompt data.
The builder reads context.user_id (tenant), while PATCH writes actor.own_id.
This is an isolated candidate observation, not an installed/live claim; no product
repair precedes FIRST full diagnostic baseline. Independent review remains due.

Revision033 binds the actual chronicle HTTP route to13new and5reused nodes.
Expected shared-corpus history, own private events, order, empty results, exact
50visible-row limits and five local-day anniversaries are literal fixtures;
foreign selectors, denied capabilities and invalid windows cannot alter state.
Six controls corrupt actual responses or persist unexpected events after a real
HTTP success/refusal. Only the clock is controlled; this is not live delivery.
Two plain candidate failures remain: a UTC knowledge timestamp inside the local
seven-day boundary is omitted by lexical comparison of differing offsets; a valid
persisted timestamp event earlier today is excluded by the date-only upper bound.
The timestamp fixture uses the official storage API, whose timeline projection
explicitly supports datetime values, not a fabricated invalid SQL row. Other
accounts, knowledge, graph, event times, privacy owners and notifications remain
exact across reads/refusals. Product changes wait until FIRSTfullbaseline;
independent instrument review remains pending.

Revision034 binds eight admin read surfaces: filtered knowledge list, tags,
corpus timeline, inspection, version diff, confirmed mentions, raw source search
and containers. Eighteen existing behavioral nodes are reused;23new nodes add
literal contents/membership/limits/history/source provenance, scoped administrator
attribution, per-route permission refusals and preservation of all business rows.
Seven controls corrupt actual returned totals/history/source/diff, suppress the
real audit writer or persist an unexpected foreign-document edit after HTTP
success/refusal. Current source is unchanged. The first metadata diff oracle
mistook the documented `map` discriminator for `mapping`; positive and actual
wrong-diff control were rechecked after correcting the oracle. Audit canaries
cover all seeded document/person/container names. Independent review remains
pending; focused diagnostics give no fullbaseline/canonical/live credit.

Revision035 binds the three existing capability/preset routes with25new nodes,
16actual fault controls and8reused behavioral nodes. Astra independently found
that a post-HTTP write granting a sibling account preset-management rights escaped
the original create oracle. The repaired harness checks full account rows (only
configured-owner auth activity times normalized), all overrides/identities and
raw preset capability rows. Four additional controls corrupt real persisted
rights after success/refusal, insert orphan capability rows or change account
creation time. Audit history is preserved; new preset timestamps are bounded.
Three source failures remain plain: update audit omits before, delegated creation
records shared tenant as creator, and delegated update rewrites a custom policy
assigned to the canonical owner. The latter proves a stored owner-policy mutation,
not owner-data access. No product change precedes FIRSTfullbaseline.
The original Astra task mistakenly requested DELETE /api/admin/presets/{key}.
No such registered entrance or existing feature contract was found; Sol explicitly
marked it prospective. Its proposal stays in the original handoff, outside this
revision's new canonical cases. No existing required case/surface was waived.
Astra first supported-scope replay plus independent escape probe:19PASS3FAIL17.53s;
repaired25nodes:22PASS3plainFAIL19.08s including16controls. Independent review of
Astra repairs is pending. These are focused diagnostics, not FIRSTfullbaseline.

Revision036 closes Sol PERSONAL001's preservation gap in three standalone
identity cases. Owner-person filtered GET, delegated person link and delegated
owner-bound rebind now compare all account/override rows immediately after HTTP,
before their retained source-failure assertions. GET and delegated link also
preserve unrelated identity rows and exact expected link cardinality. Eight real
post-request controls change a sibling preset or override on each path, or delete
a neighboring identity during GET/delegated POST. Each must fail its preservation
assertion specifically; an existing audit/provenance/refusal failure cannot make
a control pass. First3changed+8new:8PASS3plain sourceFAIL9.23s. Product source and
old node IDs are unchanged. Independent acceptance of this repair is pending;
focused results do not count as FIRSTfullbaseline or live acceptance.

Revision037 binds knowledge edit, version restoration, soft deletion, purge and
purgeable inventory to30new real HTTP nodes with13actual fault controls and12
existing behavioral cases. Literal intended row/response/history changes, creator
provenance, exact affected-row purge receipt, all neighboring business/graph rows,
permission/owner/invalid/replay boundaries and raw/durable privacy audit are checked.
Existing file/vault/FTS/aliases/dedup and both audit-failure boundaries remain bound.
Sol found that a relation created after a knowledge GET escaped revision034's
snapshot. Relations, revision history and candidates are now tracked, with two
actual GET/403 relation-write controls. Only the durable logical observed_at clock
is normalized: ordinary credential/audit transactions advance it; graph contents,
history and cleared transaction batch fields remain exact. This is not a graph
clock conformance test; its existing dedicated obligations remain unchanged.
First30-node predecessor had29nodes:22PASS7oracleFAIL28.04s. A failed edit script
then accidentally replayed unchanged52nodes:44PASS8FAIL49.53s, including a stale
writer-control timestamp assertion; no source fix existed for that replay. The
control now compares intended persisted fields before validating the timestamp.
Adding graph context without normalizing its documented logical clock caused
39FAIL15PASS50.68s; this was one oracle root, not39product defects. After correction,
54PASS1oracleFAIL53.26s remained: closed audit schema hides restored_from_version.
Expected raw audit/HTTP version stays exact; persisted audit expects private field
count. The corrected restore and12reused nodes passed13/13 in10.26s;169metadata
checks passed36.10s. Final collection27985, scratch20946MiB;85bindings verify,
221structural gaps and86required NOT_RUN remain. Refusal
controls require actual injection and cannot pass early on a legitimate auth.failed
audit. Independent review remains required; no product edits/fullbaseline/live
acceptance credit follows from these focused diagnostics.

Revision038 adds three real entity review reads with31nodes/11actual controls and
six reused behavioral tests. Queue and group windows, literal names/IDs/types and
decided statuses, empty/error/authority boundaries and all business/graph/audit
preservation are observed. First35nodes15PASS20oracleFAIL32.10s: empty audit
payload is NULL, not empty JSON; append-only audit needs corruption at its real
writer, not a forbidden SQL UPDATE. Repaired29nodes23PASS6FAIL28.57s exposed a
sentence-final period dropping exact existing names. The actual matcher rejects
a following dot, though the phrase-scanner contract strips sentence punctuation.
Keep two separate required period scenarios plain FAIL. Semicolon positive
fixtures allow each actual fault control to reach its intended assertion; final
31nodes29PASS2plain sourceFAIL30.36s. Six old cases passed in the first run.
No product repair or FIRST/fullbaseline/live credit. Independent review pending.
Sol independently accepted knowledge037:55PASS9.70s,13bindings22controls; the
relation-write preservation gap is closed within the documented clock scope.
Metadata169PASS42.65s; Ruff/format/diffPASS. Collection28016; non-nightly scratch
21194MiB, prior declarations unchanged.88bindings verify,218structural gaps and
89required NOT_RUN remain. Registration initially stopped after inventory write
on an all-tier scratch sum; verified/resumed using the existing non-nightly bound,
no metadata test was run on the aborted registration.


Revision039 integrates repaired Chromium oracles for12of17 admin tabs, bound to
37 exact nodes in35functions. Activity/chats/graph/sources/timeline stay gaps;
sidebar membership is no behavioral proof. Eight actual response/DOM/persistence/
artifact faults and five real startup/normal-cleanup nodes accompany the positive
user paths. New declarations are browser/change/300s and32MiB per parameter,
a conservative scratch estimate, not a measured consumption; all old rules and
parameter digests remain unchanged. Collection28053; non-nightly scratch22378MiB.

Grok UI002 first31nodes28PASS3FAIL68.55s, final30PASS1FAIL68.83s retained privately.
Astra and independent Sol static review found the remaining 404 expectation
incorrect: administrator inspection deliberately preserves soft-deleted document
history. Its replacement checks exact own history/content/deleted_at/version2+1,
foreign state preservation and exclusion from the refreshed list. Fake pre-call
failure hooks were replaced with actual seed/launch/goto failures, original plus
cleanup BaseException preservation, and observed closure of owned socket, server,
thread, browser, driver and descendant birth identities. Browser startup is
bounded; uncertainty in tracked-process inspection fails rather than claiming
cleanup. No product changes were made.

Astra first repaired35nodes PASS71.43s. Strengthened compacts and cleanup positives
plus two new actual faults PASS4/4 in8.02s: exact HTTP/persisted empty-day counters,
literal UI values/dashes and real empty-person membership with full relevant-row
preservation. Final five cleanup nodes PASS7.64s after exact-PID observation was
strengthened. No unnecessary whole-suite retry; final host browser/server census
empty. Independent updated review remains required. These are focused instrument
diagnostics, not FIRST baseline, live acceptance or a chosen release browser scope.

Sol independently accepts entity038:31nodes29PASS2plain sourceFAIL7.41s, no retries,
old tests or extra probes. Both final-period matcher failures remain required and
unfixed until FIRST baseline. suggestion_foreign proves same-person wrong identity,
not foreign-tenant privacy. Lab lifecycle/conflict mapping was never claimed and
expired with deadline_passed_before_claim; no work/RESULT credit or blind resubmit.
Owner is asking Grok to repair its watcher; Astra/Sol continue independent work.

Registration metadata first169nodes168PASS1oracleFAIL40.28s: an old test assumed
all canonical-pytest bindings were deterministic. It now selects the deterministic
layer and explicitly excludes browser cases from that driver's result; its
focused final replay passes. All other168checks retain their first PASS. Ruff,
format and diff checks pass.100bindings verify;218structural and300execution gaps,
101required NOT_RUN remain. The structural auditor intentionally requires actual
bound receipts for browser/native/device layers, so browser registration alone
does not reduce its structural gap count. No rule was relaxed to get coverage.


Revision040 adds7Sources and6Chats browser nodes, seven actual fault controls,
with the independently accepted owned039 fixture. Sources binds exact selected
membership/environment presence, absence of a private fixture secret from the
actual response/DOM/stored rows, persisted declaration/forgetting and preservation
of a same-named foreign source. A missing environment gets real409 and an explicit
toast without any external database connection. Controls add a secret/foreign
member to actual HTTP or remove/resurrect rows after actual operations.
Chats binds exact feed/person/thread membership and two independent labelled
pending outbound rows from two identical UI replies. Selected recipient/body,
preserved histories, no reply box without a transport and no effect from an
unsent draft are observed. Actual automatic refresh displays a newly stored
message while retaining that draft. Controls replace actual thread content or
delete/misroute actual queue rows. This proves queued intent, not Telegram delivery.

First13nodes:7SourcesPASS6ChatsoracleFAIL111.82s: the browser percent-encodes colons
in person IDs; the request matcher compared undecoded paths. Corrected exact
decoded-path comparison and encoded interception pattern, replayed only six
affected Chats nodes. Product unchanged.8function declarations add416MiB at
32MiB per browser parameter, all prior rules/digests unchanged. Collection28066;
non-nightly scratch22794MiB.14of17 tabs have bindings; activity/graph/timeline
remain unbound. Structural browser coverage still requires bound run receipts.
Independent review of040 is pending; no canonical/FIRSTbaseline/live credit.

Sol independently accepts039:37PASS25.37s,0old/probes/retries and empty owned
process census. Result/evidence/rawlog hashes reopened. Its guarantee concerns
observed owned resources, not all host processes. Prior UI001 findings closed.
Owner-reported lab watcher repair has a durable matching-nonce idle-probe claim
and RESULT. New normal mapping002 job was actually claimed09:24:06Z after submit
09:23:28Z; it is independent lifecycle/conflict mapping, not a retried expired001.

Final affected Chats6PASS15.76s; Sources7 retain first PASS. Metadata169PASS41.88s;
Ruff/format/diffPASS; postrun browser/server census empty.102bindings verify,
218structural/300executiongaps103requiredNOT_RUN. Lab mapping002 returned its
normal RESULT09:33:33Z: eight actual routes mapped,10model-free nodes PASS9.28s.
Mapping/confirmation/log/JUnit and exact source hashes were checked, actual JUnit
node IDs equal the declared10. Five primary HTTP gaps and per-route authority,
refusal/audit/state boundaries remain. This establishes receipt of a normal task
following watcher repair, not a guarantee against future transport failures.

The first040freeze precondition stopped before writes because it compared only
unstaged paths: the real private-worktree index now stages exact039. Origin is
unresolved, preserved in a private index snapshot/reconciliation. HEAD remains
base1bfd839c, and staged+unstaged+untracked working differences contain exactly
50authorized harness paths, no product paths. Freeze now checks the entire diff
against base, preserves that observed index and uses a separate private index.
This required no source/test replay and did not waive the path allowlist.


Revision041 binds representative paths on the last three tabs: Activity, Graph,
and Timeline. Four functions/12 Chromium nodes include nine actual controls.
Activity observes the selected person's exact seven-day request, raw membership,
previews, summary/source/day counts and DOM, then opens a literal raw preview.
The browser Date is fixed; server clock and browser timers remain real. Graph
observes exact own nodes and both asserted/cooccurrence edges, type filtering,
named-node inspection and filtered local focus. Current overview total counts
the whole archive, while shown/matched counts describe the filtered subset.
Timeline observes exact dated items/year buckets and undated count, clicks the
2024 bucket to request/render exact months, then resets the date window.
All three preserve the full relevant business/graph/history/messages snapshot;
documented relation context observed_at is normalized and only the legacy-owner
authentication updated_at/last_seen_at pair may advance within bounded server
time, staying equal. All other owner fields and all other users stay exact.
Existing audit rows
are intact; relevant graph/activity/knowledge read actions have exact counts,
owner actor and selected-user target without content. Ancillary audit actions
are not claimed exhaustive. Each positive oracle also runs against an actual
HTTP membership corruption, an unexpected foreign-relation write after a real
response, and omission of its read audit. Controls must reach named assertions.

First12nodes all errored in fixture setup: a positional KnowledgeObject argument
populated entity_id instead of content. Keyword seed correction gives second
9PASS3graph failures24.79s. Graph's unlinked peer was excluded by the current
knowledge-backed overview; seed now links it to a separate existing document.
Affected4graph:1PASS3oracleFAIL11.10s exposed total's documented unfiltered-archive
meaning; corrected literal total=3. Remaining3graph:1PASS2FAIL9.49s exposed
legacy-owner authentication activity timestamps in the business snapshot. One
focused diagnostic1FAIL4.27s confirmed only the users table changed; server.py
owner-token authentication calls ensure_user, whose paired timestamps advance.
Oracle now permits only this pair within a monotonic bounded server-time interval;
all other fields stay exact. Full final12 replayed because this shared helper
changed. All first/repair/diagnostic logs retained. No product changes.
Collection28078; four new declarations add384MiB at32MiB per parameter;
non-nightly scratch23178MiB, prior rules/parameter digests unchanged.17of17tabs
now have representative bindings; this does not establish exhaustive features,
supported-browser acceptance or canonical execution. Structural browser gaps
still require bound execution receipts. Independent041 review pending.

Sol independently accepted040's13Sources/Chats nodes10.42s, zero old/probes or
retries and clean owned census. Grok delivered lifecycle/conflict12new nodes
(first11PASS1locatorFAIL, final12PASS12.57s); hashes, both JUnits and declared IDs
verified. That separate file remains unintegrated and under independent Sol
nonUI review. No FIRSTbaseline/final/live credit from either focused package.


Revision042 adds one deterministic case for four source HTTP routes,13new nodes
and10actual corruption controls. Selected and default-account list membership,
public fields/environment presence, real owned SQLite schema tables/columns/types
and unchanged bytes of both own/foreign source databases are observed. Missing
source404, missing environment409, invalid declarations400, anonymous401,
explicit capability denial403 and delegated-admin owner-target mutation403 are
checked with unchanged relevant business/raw/knowledge/graph/history/source state.
Scoped credentials avoid legacy-owner user-clock normalization. The source reader
has explicit data.read; it is not treated as an ordinary-user default capability.

An explicitly authorized person creates their own source; a different owner then
replaces and forgets it. Every target field, original creator/time and same-named
foreign/sibling rows are checked against closed expected deltas. Initial creation
on behalf of a different person is outside this provenance scenario. Audit names
each actual actor/action and a source-name HMAC target, independently computed
from the private fixture key without using production sanitizers. Exact body-free
payload shape and historical audit prefix are required; connection values never
appear in actual HTTP, source rows or audit. Only source-related success/read
audit actions are claimed exhaustive; security denial logging is separate.

Controls modify actual list/schema/declaration HTTP, replace a list member with
a real foreign registered source, corrupt persisted creator/upsert time or delete
a foreign sibling after real operations, write the actual source SQLite file,
omit real audit writes or log a wrong target. Each must hit the positive oracle's
named assertion; injected faults are proven executed. Connections explicitly close.
PostgreSQL/MySQL connection success, external network, browser, models and live
acceptance are not claimed by these SQLite instrument nodes.

First12:6PASS6oracleFAIL11.45s because data.read was absent on the synthetic reader
and expected audit values ignored existing privacy projection. Explicit grant and
independent body-free/HMAC expectation corrected; second13PASS12.46s. Strengthened
foreign-source control alone1PASS2.50s. Creator/operator separation affected seven
write nodes, which were replayed; unchanged read/refusal nodes retained. Existing
nodes not replayed. Four declarations/13parameters add104MiB at8MiB each; collection
28091, non-nightly scratch23282MiB; all prior rules/parameter digests unchanged.
Independent042 review pending; structural binding is not canonical execution or
FIRSTbaseline/live acceptance. Sol's two actual false-negative probes rejected
Grok lifecycle/conflict001; missing audit and unchecked changed-row corruption
are being repaired in separate lab002, not incorporated into this frozen suite.


Revision043 adds the deterministic audit HTTP case:12nodes, two positive scenarios
and ten actual corruption controls. Exact actor-filtered whole stored rows, tied
ID ordering, totals, paging and anchors are checked across a real newer insertion.
Owner and delegated reads name the actual inspector; one new audit event binds
actor, target, payload and independently HMAC-computed client request correlation.
An unknown-account target uses its independently computed opaque reference. Scoped
credentials avoid legacy-user clock allowances. The full relevant business/graph/
history snapshot and historical audit prefix remain exact. Anonymous/denied/revoked
readers and invalid query bounds cannot read or mutate business data.

Faults corrupt actual HTTP rows/order/count/anchor/payload, omit or misattribute
real audit writes, change a real knowledge row after reading, or grant permission
after a refused request. Each reaches the same named positive assertion and proves
injection. The seeded rows use real generated IDs and canonically precise timestamps;
this tests HTTP delivery of persisted rows, not the entire storage privacy writer.
Timestamp-only paging under equal-time concurrent insertions, global unanchored
first-page concurrency, all audit payload classes, UI/live/model paths are outside
this case. No accepted baseline or exact-release execution is claimed.

First12 setup errors11.71s: untrusted fixture IDs were projected. Corrected generated
IDs/request HMAC; second6PASS6oracleFAIL12.89s due expected timestamp precision.
After canonical timestamps, third11PASS1oracleFAIL12.75s; independently corrected
unknown-account target HMAC and replayed only the affected positive:1PASS2.55s.
Three inventory declarations/12parameters add96MiB; collection28103, non-nightly
scratch23378MiB. Prior rules and node digests unchanged. Independent043 review pending.


Revision044 repairs the source HTTP privacy oracle rejected by independent Sol042
review. The submitted invalid direct DSN is now a canary for actual HTTP/audit/
source rows, including every anonymous/denied/owner-protected refusal body. Invalid
dsn_env400 has a closed generic response; the test no longer accepts only a reason
substring. Three controls reflect that exact input into real400/401/403responses,
keep their actual statuses and must hit source_connection_secret with injection
proof. Affectedpositive+3controls passed4.83s. Sol's unchanged original probe now
fails at that assertion2.43s; its original escape1PASS2.39s remains in the ledger.
No product defect or product repair is claimed. Independent repair review pending.
Source case now16nodes/13controls; no new case/surface. Three parameters add24MiB:
collection28106, non-nightly scratch23402MiB. Only that existing function's exact
parameter digest/count/scratch changes; all other rules remain byte-semantically
identical. Old successful nodes are not replayed for this registration change.
Audit043 is independently SCOPED_ACCEPT:12PASS12.75s,0old/probes/retries, exact
source/log/evidence hashes reopened. Its documented limited scope is unchanged.


Revision045 integrates independently accepted lifecycle/conflict HTTP oracles and
eight exact route bindings. Five positive tests and twenty actual corruption
controls complement retained candidate/deprecate/resolve positives; retained nodes
were verified against collection, not replayed. Sol review002: 25 PASS21.79s,
0old/retry; both unchanged prior false-negative probes now fail at their intended
audit assertions3.16s. Full changed rows, complementary own/foreign business state,
decoded version snapshots and action audit provenance are checked. SQLite-only
deterministic evidence; no concurrency, UI, model, full or live acceptance credit.
Collection28131; six new inventory functions add200MiB, scratch23602MiB.
Quality/settings candidates remain separately pending independent review.


Revision046 adds independently accepted quality HTTP oracles:23 new nodes,
21 actual corruption controls and one retained private-quarantine usage node.
Literal totals, complete pages and candidate rows, permission transitions,
requested-person refusal privacy and independently bound404 audit provenance
are checked against actual HTTP and full persisted state. Sol repair002:10
affected PASS10.09s; original2 false-negative probes now fail at intended
assertions3.19s. Thirteen previously reviewed nodes stay unchanged and were not
replayed. Retained node membership is verified without a fresh execution claim.
Personal synthetic SQLite coverage only; no scale/concurrency/UI/model/live or
full diagnostic baseline credit. Settings, diagnostics and API metadata candidates
remain outside this revision pending independent review.
Collection28154; three new inventory functions add184MiB; non-nightly scratch23786MiB.


Revision047 adds independently accepted settings and diagnostics HTTP oracles:
11 settings nodes/9 controls and15 diagnostics nodes/12 controls. Settings checks
15 selected fields, authority, secret/path privacy and full prior audit history.
Diagnostics checks selected real collector fields, the one authorized configured
vault-path projection, privacy elsewhere, per-response state/audit and separate
query-forwarding plumbing. These are model-free tests, not actual model health.
Sol settings002:6affectedPASS6.12s,0other5/old/probes; diagnostics002:15PASS13.07s
and2independent privacy probesPASS3.13s. Reviewed leaf bytes and shared fixture
dependencies are unchanged when integrated; first failures remain preserved.
No exhaustive schema/action-code, grant-cycle, model/laptop/live/full baseline
credit. API metadata and Inbox read candidates remain outside this revision.
Collection28180; seven new function rules add208MiB; non-nightly scratch23994MiB.


Revision048 integrates three independently accepted HTTP test modules as four
cases: root redirect, docs/OpenAPI metadata, personal Inbox reads and public
health. The root redirect retains CAP-ADMIN-UI; docs/schema and health retain
CAP-OPS-HEALTH; Inbox retains CAP-GRAPH-INBOX. Existing /api/health bindings remain.
The 77 added nodes include 67 actual corruption controls. Reviewed leaf bytes
and their direct fixture dependencies remain exact; producer failures and
independent repair reviews remain preserved.

Metadata binds redirect/Swagger configuration and two selected OpenAPI operations,
five issued raw credentials and parameter uniqueness. Sol repair002: five affected
PASS in 6.44s and two disjoint sensitivity probes PASS in 3.51s. This does not
certify the whole schema, browser execution or response-header secrecy.
Inbox binds personal self/admin selection, complete selected admin envelope/item
and nested objects, raw credentials and persisted state/audits. Sol repair002:
seven affected PASS in 8.02s and two disjoint probes PASS in 3.41s. Shared-archive
and mutation behavior are outside this case's scope.
Health binds selected fields on both public aliases, anonymous/owner/invalid-token
requests and storage ok/starting states. Actual home, vault and effective Obsidian
roots are independent privacy canaries in responses and audits. Sol repair002:
ten affected PASS in 8.74s and two disjoint probes PASS in 3.05s. Full schema,
semantic supervisor, model readiness and response headers remain outside scope.

Collection: 28,257 nodes; twelve new inventory rules add 616 MiB, bringing
non-nightly scratch declarations to 24,610 MiB. Earlier rules remain unchanged.
No product logic, model or deployment changed. These are instrument checks;
FIRST full diagnostic baseline, live/model runs and final acceptance remain NOT_RUN.

Revision049 registers independently accepted reuse of 19 existing tests for four
cases and thirteen public/admin API routes. Four existing test functions were
strengthened; no new test node or parameter set is introduced.

Entity CRUD checks selected public and persisted fields, soft deletion,
undeletion and version restoration. Knowledge CRUD checks selected persisted
content and tombstones, with retained ordinary-user and shared-archive flows.
Each modified walk preserves its complete ordered audit preimage and requires
the entire new audit suffix to match the expected actions and actors/targets.
Sol repair002: two affected PASS in 3.27s and two independent prefix/missing-row
probes PASS in 3.03s. The prior extra-audit escapes are rejected. Graph HTTP
authority remains scoped to the legacy owner; cross-tenant restore supplements
exercise storage directly.

Eval checks actual seeded search results, stored gold cases, selected run
metrics and baseline, and deletion. Retrieval explain binds the actual ingested
objects to returned/discarded identities and reasons. Sol review001: ten PASS
in 6.70s and two independent baseline/discard-reason probes PASS in 3.15s.
The retained policy spy checks invocation arguments, and the privacy test checks
the nested search_explain projection. Neither establishes all storage effects,
whole-response privacy, exhaustive permissions, or dense/model behavior.

These registrations reuse the existing inventory and scratch declarations.
They provide instrument coverage; canonical execution, FIRST full diagnostic
baseline and final acceptance remain pending. Prior producer failures and
review evidence are retained separately.

Revision050 registers independently accepted reuse of eighteen existing tests in
five cases for twelve public/admin routes. The printed harness selects the
eighteen exact IDs once; the public/admin mission cases share one existing node.
No test node, parameter set, inventory rule or scratch allowance is added.

Compacts bind a real owner/day source and exact counters/incident wording, with
foreign-user and neighboring-day controls. Missions bind goal/creator/list
identity and GET-persisted cancellation; admin oversight retains audited reads,
owner refusal and ordinary-person cancellation readback. Sol: ten PASS8.43s and
two independent original-positive cardinality probes PASS1.28s.

Export binds actual artifacts, downloaded bytes and known owner raw/knowledge
content; file delivery binds bytes/MIME/disposition, then quarantined refusal
and unchanged audit. Sol: eight PASS6.50s and two independent audit probes
PASS3.29s. The opaque export audit-reference shape does not prove cryptographic
filename binding. Three retained privacy nodes exercise storage directly.

Prior failures and scope limits remain recorded. No complete backup/import/
restore, exhaustive authority/schema/effects, live/model or full-gate claim is
added. FIRST full diagnostic baseline and final acceptance remain pending.

Revision051 adds three independently accepted cases using nineteen existing test
IDs for twelve notification, container and timeline routes. Three leaf files
match Sol's reviewed bytes; two differ only by Ruff import ordering/coalescing
and whitespace, with unchanged imported symbols and all other AST. Direct
fixtures and retained helpers match their reviewed versions.
Two notification positives now use signed synchronous HTTP instead of direct
async calls; their names and parameter IDs remain unchanged. The printed plan
selects each of the nineteen IDs once. No inventory rules or scratch are added.

Notification checks bind pointer identity, downloaded bytes, claim and ACK
persistence; six revocation nodes are direct-handler supplements. Graph checks
bind selected owner membership, parent edges, statistics, persisted dates,
normalized timeline windows and exact audit/refusal state; five storage nodes
are supplements. Sol ran 11/8 exact nodes and two disjoint probes per candidate.
These cases retain their stated limits: synthetic archive bytes do not prove ZIP
validity, and selected owner HTTP does not prove full authority, schema, privacy
or live behavior. FIRST full baseline and final acceptance remain NOT_RUN.

Revision052 registers five independently accepted cases using eight existing
nodes for eight selected knowledge, monitor and reflection routes. Six routes
gain their first case; existing container/tag bindings remain. Four reviewed
leaves are byte-exact; one differs only by formatting, with its full AST exact.
Direct fixtures and the prior shared-archive CRUD function remain unchanged. No node IDs, parameter sets, inventory rules or scratch are added.

Knowledge binds source identity/excerpts, the March page/total and dual-person
own membership under a shared query/window. Monitor binds persisted create/stop
and exact audit/refusal state; reflection binds the deterministic own digest
and literal message. Three direct storage/build tests remain supplements.
Prior producer provenance limits remain explicit. Registration supplies no
shared-archive, worker/model, exhaustive authority/schema/privacy or live credit.
FIRST full baseline and final acceptance are still NOT_RUN.

Revision053 registers one independently accepted signed bridge-events case,
nine existing HTTP node IDs and POST /api/events. The reviewed leaf and all
direct fixtures match exactly. Exact response/persisted journal identity, prior
row preservation, literal payload values and bounds, five allowlisted types and
selected 400/403/401 privacy/state checks are bound. Sol's nine-node run and two
actual original-call response-ID/refusal-journal controls passed. Ordinary scalar
equality is not strict type fidelity; signature/replay/chat authority, nested
payload, retention, producer transitions and live Telegram remain outside scope.
No new nodes, inventory rules or scratch; FIRST full baseline and final NOT_RUN.

Revision054 registers two independently accepted approval HTTP cases: six unique
existing nodes, ten case memberships and two newly bound routes. Reviewed leaves
are exact; retained notification body and direct fixtures/helpers are unchanged.
Own list membership, selected decision/replay/SQL state and complete audit deltas
bind to original positives. Sol ran the final six-node derivative once and proved
actual self-link and post-refusal audit-append sensitivity. Two probe setup
failures remain recorded; the valid refusal proof starts from an empty audit.
Raw LAB001 worker/provenance limits remain; raw LAB002 greens are not relabeled
as final derivative execution. No exhaustive schema/role/privacy or live credit.
No new nodes/inventory rules/scratch; FIRST full baseline and final NOT_RUN.

Revision055 registers the independently reviewed cleanup HTTP workflow: one
existing node, one case and two routes. The exact reviewed leaf binds closed
preview pages, literal risks, selected refusal/audit preservation, deduplicated
apply, concrete version/reviewer/raw provenance and replay state. Sol's sole
testcase passed (JUnit4.657s); its process exited1 after completion because a
precreated collection path collided with O_EXCL. This is scoped review evidence,
not a clean gate process. Two disjoint actual replay-provenance/risk controls
passed5.57s. Prior author/lab failures and missing source records remain. Inbox
proof is the returned expected row plus non-target preservation, not every new
target row. No new nodes/rules/scratch or full/live/model acceptance credit.

Revision056 registers independently reviewed relation-candidate HTTP list/review:
one case, two routes and four existing selectors (two strengthened bodies plus
two retained identity/write-only HTTP supplements). Literal ordered pages and
cards, selected candidate/reviewer/edge, full ordered audit delta, refusal and
replay state are observed. Sol4PASS6.173s/exit0 and two independent actual
edge-only SQL/extra-audit controls2PASS4.56s; the edge control preserves six
selected candidate fields/reviewer, not a complete candidate-row snapshot. B
preserves selected candidate/own relations, not endpoint entities/all tenants
on writes; stronger A preservation is limited to GET/refusals. Producer missing
probe argv/environment and pre-run Ruff records remain explicit. No new nodes
or rules, added scratch budget, product/model/live/full acceptance credit.

Revision057 registers the independently accepted conflict-list HTTP workflow:
one existing GET node, one case and one route. Exact literal triage labels and
numerics, closed ordered pages, tenant membership, full selected raw/knowledge/
conflict preservation and ordered audit prefixes/deltas are bound. Refusal
401/403/400/422 envelopes and selected raw/token canaries include huge limits.
Sol1PASS3.852s/exit0; two independent actual SQL source-ref/extra-audit controls
passed4.58s. Positive titles/summaries are public; no exhaustive privacy or
POST-decide/DIRECT/Telegram coverage. Author initial wrapper failures receive
zero control credit; original missing probe argv/env and pre-run Ruff remain.
No new nodes, inventory rules/scratch budget, product/model/live/full credit.

Revision058 registers one independently reviewed text/import HTTP case: five
existing nodes cover POST /api/ingest and POST /api/import. Exact typed and
literal Raw/Inbox/KO rows, policy, tenant/actor provenance, cardinality, replay
and unfiltered import audit are asserted. Sol five nodes PASS7.27s; optional
controls0/2 NOT_RUN earn no fault credit. Refusal keeps its earlier limited
scope; missing separate pre-run import/conftest/runner identities do not become
full runtime attestation. Prior sanitizer failure, narrow control evidence and
publication limits remain. No new nodes, inventory rules, product changes or
canonical/live/first-baseline acceptance are claimed.

Revision058 also registers one resolution/merge HTTP case with three existing
nodes and nine API routes. Exact candidate/member pages, accept/undo histories,
target tuples, five whole-table preservation guards and complete audit prefixes
are retained. Sol accepted source004 and three positives; controls007 verifies
two committed non-target faults through the original assertions and retained
fresh readonly snapshots. These controls establish only their stated fault
sensitivity. Earlier source, run, Ruff and deadline failures remain recorded;
this registration does not grant canonical execution or product acceptance.


Revision059 registers R10-ADMIN-LINK-REVIEW-HTTP with two existing HTTP selectors
for PATCH /api/admin/entity-links/{link_id}, under its existing CAP-ADMIN-UI rule.
Sol FIRST002 SCOPED_ACCEPT ran both once: pytest2PASS4.99s, process5.696782s.
The accepted leaf pins selected link/reviewer, accepted relation candidate cards
and rows, rejection without new candidates, selected ten-table replay/preservation,
and complete ordered audit prefixes/deltas with retained authority/refusal checks.
relation_revision_context.observed_at remains present in snapshots but is excluded
from equality; clock integrity, monotonicity and rollback are not claimed.

No new controls were requested or run. Earlier failures and late pre-repair
reviewed_by/extra-audit controls retain their incomplete immutable/fresh-readonly
limits; they gain no new fault credit. Request-ID format/distinctness and raw audit
JSON/SQL NULL representation remain outside the assertions. No personal link or
Resolution route is added. Existing case bindings, required layers, node IDs and
inventory remain intact; this registration grants no canonical execution, full/live,
product, baseline or GO acceptance.

Native receipt auditing accepts `--native-receipt`, `--native-receipt-sha256`,
`--native-context`, and `--native-context-sha256` together with `--audit-only`
and `--collection`. Context is a separate private canonical JSON file outside
the run directory, with closed schema `friday.r10-native-context.v1`, `base_sha`,
the seven native `identity` fields, `run_id`, ordered `case_ids`, boolean
`secondary_enabled` and `secondary_mode`. Expected values and both digests must
be retained independently of the output being audited. The CLI does not derive
expectations from summary.json or claim that file placement proves their origin.

The reader checks current candidate/tree, exact native source and suite digests,
and context again before reporting. Gate/native evidence must share candidate,
tree, wheel and ancestor base. Only validated selected native case layers earn
credit; explicit FAIL and root diagnostics remain visible, root-suppressed PASS
does not count, and unrelated cases stay NOT_RUN. No receipt still means NOT_RUN.
Structural `valid` remains distinct from `execution_complete`; GO stays false.

The caller must use the explicit acquisition path below and retain its context
before auditing a run. Legacy internally generated run IDs provide no such
context. Synthetic reader tests supply no native/browser/secondary execution
evidence. Existing controller-attested cleanup and elapsed-time, runtime-chain
and unindexed secondary-CA limits remain.

Explicit native predispatch context acquisition accepts run_native keyword arguments
run_id, base_sha and context_path together; the journeys --run-live CLI exposes
--run-id, --base-sha and --context-out, plus repeated --case-id in caller order.
Legacy calls omit this group and retain generated run IDs without external output.
The base must be a distinct ancestor of the frozen candidate. The context and its
.sha256 companion must be new, outside the candidate and run directories, in an
existing canonical owned0700 directory; existing destinations and modes are preserved.

After building and binding the wheel, installed site, source snapshot and native
suite, the controller publishes canonical0600 context bytes and their digest before
readiness. It retains all seven run identities, explicit run/ordered selection and
secondary policy; it rechecks both outputs before every worker. Later failures keep
the successfully published context. No unsafe general battery file helper is used
for these external files. The caller must retain this predispatch context and digest
for the acceptance CLI; reconstruction from the outcome is unsupported. Publication
establishes controller acquisition timing, not independent authorship, process
attestation, per-worker runtime identities, execution acceptance or release GO.


Revision060 is a prospective composition, pending parent integration checks.
It preserves all058/059 bindings while carrying
the same immutable native composition4b0376742b102793ef85a6f3771efd37849dfe88.
Native FIRST003 independently passed the exact126-test batch; Astra verified the
bound receipt, actual process environments and before/after artifacts. This
separate candidate result does not establish an accepted060 freeze, refreshed
inventory, combined060 execution, model/live/product acceptance or GO.

R10-CONFLICT-DECIDE-HTTP adds only the personal POST
/api/kg/conflicts/{conflict_id}/decide route and its one existing selector. Sol
FIRST003 SCOPED_ACCEPT ran it once, process4.617660s, without new controls. Selected
conflict/triage persistence, suggested-list hiding, replay/refusal preservation and
ordered audit checks retain their reviewed scope. Exact audit ID and timestamp
branches have positive evidence; prior002 control/race limits remain and no fresh
ID/time fault sensitivity is inferred. Admin decide routes and unrelated actions
are outside this case. Its old node declaration and inventory membership stay intact.


R10-ADMIN-ENTITY-WRITES-HTTP prospectively registers only admin entity POST, PATCH
and DELETE against three exact tests in test_admin_entity_mutation_http.py. The
independent Sol SCOPED_ACCEPT source is candidate
5e4347249ec6dd8ff2b133b7ac75158fb0d3c0de; Astra verified the bound receipt,
three exact PASS outcomes, process environments and before/after artifacts.
The scope covers literal public cards, selected entity/version persistence,
ordered sanitized audit deltas, canonical generated IDs/times and the retained
ordinary-person, foreign-target and repeated-delete refusals. No all-role,
relation-invalidation, concurrency or broader privacy claim is added. These three
new test declarations await inventory and collection; source composition supplies
no combined execution credit, accepted freeze, live/model/product acceptance or GO.


R10-FEEDBACK-WRITE-HTTP and R10-ADMIN-FEEDBACK-HTTP prospectively bind personal
POST /api/feedback and admin GET /api/admin/feedback to their two existing tests
in test_maturity_070.py. Separate cases preserve the existing Graph/Inbox and
Admin UI capability boundaries. Accepted candidate
881664c4b2fabb7ba9dde6efada3e4744e71bc06 supplies the exact reviewed leaf.
Sol FIRST002 ran those two selectors once: 2 PASS, process5.301681s, no controls.
Its narrow F1-F3 acceptance closes actual-second UTC formats, complete anonymous
audit rows, and the independently defined current-feedback row checked against
pre-call SQL. Prior selected replacement/history/tenant/refusal scope remains.
FIRST0012PASS remains REVISE, including its initial bare-shell zero-test failure;
older lab attempts, controls and provenance limitations remain historical.
Revision060 is still prospective: no combined execution, inventory refresh,
accepted freeze, broader privacy/global-clock, live/model/product or GO credit.


R10-OBSERVER-SNAPSHOT-HTTP adds only the hidden observer GET and its existing
real-lifespan owner/admin/remote test. Sol FIRST001 independently accepted one
PASS, process5.241630s; Astra checked65 indexed artifacts and193 bound references.
Typed nine-field counts distinguish physical hidden outbound state and stopped
bridge queues; selected state and canary checks retain their narrow scope. The
initial parent503 fixture-reader failure and repaired PASS remain separate.
No new control, broad lease/role/privacy or sidecar-race proof is inferred. This
prospective registration supplies no combined060, full/live/model or GO credit.

The production observation HTTP binding reuses the existing real-lifespan test. Its exact body, independently pinned schema/process identity and seven selected SQL tables cover owner success and missing challenge rejection. Independent FIRST001 passed one selected test; the parent fixture failure and prior formatting stage remain separate evidence. This is a deterministic route check; deployed runtime, transient writes, other roles and full battery acceptance remain unverified.
