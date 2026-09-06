# Friday: canonical project backlog

Updated: 2026-09-06 (reported production `0.208.55`; C1 source corrections and whole-product reliability work active; historical S3 witness preserved; Pandora unchanged)

This is the project's only backlog and mutable status register. It owns the
current production identity, execution order, acceptance gaps and owner actions.
Architecture, operations and acceptance documents are immutable design inputs;
they must link here for live state and may not carry a competing task list.

Old agent task files, handoffs, dated reports and superseded status registers are
kept in Git history, not in the working tree. A task discovered anywhere else is
either merged here or discarded before that source is removed.

The 2026-09-03 post-backlog audit snapshot (`main` `43a16c8b`, source
`0.208.4`, recorded production `0.208.1`) is expired. The later repository record below supersedes it. C1 has not re-probed the
live host, the laptop or Telegram. Do not copy historical release claims
forward or treat a source correction as a deployed release.

## Current production identity (last repository-reported)

Branch: `main`. Source corrections below are not a sealed or deployed release.

Golden-journey receipts remain bound to the historical `0.208.1` root below
until validly rebound. The last reported running sealed sibling is Friday
`0.208.55` at
`e60860eaa89827bc5fc58d7c4f1c47514caaf2d4` (tree-file
`706aee22e4b1a435e6ae4969d41936f603cd9863ac1e2c2535e1cfe878bb80b7`, wheel
`c834a45cc3159ea082cf002ec1dd6e0b263c87047ea1b54fe6e29683633daf1a`, journal
`clear`, predecessor `0.208.54` /
`9c8a4eed2858abd50bdcf27d7aab1b75f687f61c`; this hop's journal fallback
is the same `0.208.54` sibling). Trusted-CA health reports
`version=0.208.55` `status=ok` `requested_mode=assist`. S3 consumed
witness remains the durable 2026-09-06T05:25Z turn on then-live
`0.208.51` assist (user `msg_d145ce5796b54ae1`, assistant
`msg_df97d09e4d2b4a2f`, graph `graph_866d37c2794dcc1a`, promoted-product
`evt_1827c6dff24b40b4`). Compact health after the `0.208.55` process
epoch is `requested_mode=assist`, `effective_mode=off`
(`material_loaded_not_accepted`), `invoked_total=0`,
`publication_total=0`, `event_success_total=0`,
`last_promotion_reason=none`; that is process-epoch compact, not a
missing witness. Do not re-activate `e60860ea` or `9c8a4eed`. Next
product sibling that changes friday sources is `assist_to_shadow`. N1
Telegram surfaces and
one-final-carrier packing remain live. N2 live `_web_research` plans a
2–8 query public mission, executes complementary queries on
fact-bearing public sources, and observes remaining N2 gates on the
admitted report without inventing claims; empty research does not
attach those keys; `SEARCH_BLOCKED_PRIVATE` never becomes a query
plan. `_web_search` and `_web_fetch` refuse `BLOCKED_PRIVATE`.
File+web comparison and `POST /api/ingest/url` consume
`BLOCKED_PRIVATE`. Private N2 self-score is Friday's own gates, not
Gemini parity; do not claim Gemini parity without a paired scored
set. N3 `/coding` static inspect, isolated-worker boundary, admitted
archive extract, isolated untrusted `py_compile` / unittest,
prompt-to-small-project, one-final `friday-source.zip` pack, digest
and overwrite observation on extract, Coding Mode
view/plan-gate/carrier, and upload-modification EMPTY observer remain
live through `0.208.55`. Execute/run of uploaded programs stay
fail-closed. This is not a safety certification. Upload-modification
apply never rewrites uploaded project files. N4 store-backed mixed
journeys remain live through `0.208.55`. N5 implementable seam
extract is closed, the maintainability ratchet remains standing.

- Production: immutable activation `phase=clear`; backend and Telegram bridge
  active; writer target `candidate`. Retention admission remains honestly
  `review_required` and grants no apply/delete authority. V12 `canary_ready`
  with verified, installation and effective context all exactly `40960`.
- Secondary: accepted/live GPT-OSS profile `gptoss20b-2335df…`; after the
  `0.208.55` cutover `state=healthy`, `mode=assist`,
  `available=false`, supervisor `closed_reason=admitted`. `/api/health`
  `cooldown` is the in-process circuit, not laptop Docker liveness.
  Compact health still hides `circuit_retry_after_sec` and
  `last_failure`; use `GET /api/admin/diagnostics`. S3 consumed
  witness exists; do not fabricate additional traffic. Primary-only
  behavior is preserved when the laptop is absent.
- The reader-first body-free document-passage contour is fully converged: 1,720
  current parents and 16,359 child passages, with no pending v3 backfill. The two
  formerly invalid sparse-text v2 sources were repaired by the released v3
  topology; no document body is duplicated in the sidecar.
- The body-free conversation-passage writer and lexical lane are live. The
  first two observed bounded worker ticks advanced 66 authenticated anchors;
  restart-safe production backfill remains active and keeps every unfinished
  projection explicitly `backfill_pending`.

### Historical golden-journey receipt root

- Historical implementation head: `8b6a8c13ce54b8b07192cb6f5b820953da4efcb5`.
- Historical receipt root: Friday `0.208.1` / `8b6a8c13ce54b8b07192cb6f5b820953da4efcb5`;
  tree `5587e4c3dd45e2b85191b8d39dd0268dc7fbf046526761af6d485297d2be8f82`;
  wheel `1cadf5769b87f9cdc152729183ddf3a1d6ef4a8ff3192d48d1a5b538e856872f`.
- That root's runtime predecessor: Friday `0.208.0` /
  `75b165a23809dfcc7445311e2dc896c98ce3df00`; tree
  `9d1c49da576e58e73ec1570d3e4c7e1ea7ebca2d44cc1e7482ef418c9ec89315`.
  Schema-capable fallback: Friday `0.207.90` /
  `7abb3c5e3fb29bdc7c53bf923f8b218fa26f07e9`; tree
  `c1c29331db489ad1c56080d70a8c37d4051b4752f1309dba9c0a012099ebcae5`.
- Recorded schema: 50. These are historical receipt identities, not current-host observations.

## Active package

The owner extended this mission on 2026-09-06: repair existing capabilities,
improve their real composition and reliability, and continue the implementable
backlog. Do not expand the feature catalogue merely to add components. Pandora
is excluded. The dated live summaries below describe implemented layers, not
acceptance of all N1–N5 user outcomes. C1 is the current source-only package.


The old S0–S6 implementation queue has converged on production `0.208.11`.
S4-R8 formal cutover is code-owned `accepted`: dialogue offers `archive_search`;
`memory_search` / `source_search` / `message_search` remain internal. Exact
window, temporal and graph lanes dispatch through that facade; a generic
continuation cannot mint a fresh exact selector. S3 assist-controller is
deployed; the observation-bound consumed witness landed on live
`0.208.51` assist and remains durable through live `0.208.55`. S5 40k lease
and S6 recovery/browse paths stay live.

The live product queue is N1–N5. The shared operation-progress contract is
live. Production `0.208.55` keeps ordinary Telegram `/chat` status,
Engineer status, file-album DOCUMENT status, Engineer FILE/ARCHIVE
carrier, observed web/archive chat status, one-final-carrier packing,
owner-private `/coding` static inspect, isolated-worker boundary,
admitted archive extract, isolated untrusted build/test of extracted
uploads, prompt-to-small-project, one-final source archive pack,
digest/overwrite observation on extract, Coding Mode
view/plan-gate/carrier, upload-modification EMPTY observer, live
`_web_research` mission/gates, store-backed mixed journeys
(Telegram MIXED when PROJECTED), and one delayed secondary startup
re-probe after cooldown. Execute/run of uploaded programs stay
fail-closed. This is not a safety certification.
Upload-modification apply never rewrites uploaded project files.
Gemini parity is not claimed; it stays parked in Pandora. S3 consumed
witness is closed; live assist is restored on `0.208.55`. N5 ratchet stays standing. Physical Android, P0H
deletion, off-machine mirror and provider-credential rotation remain
owner-parked.

## Operating rules

- Release small independently reversible packages to `main` and production.
- Do not use Docker to certify primary Friday. The laptop inference node keeps
  its separate Docker contour.
- Do not touch the Obsidian companion plugin without a separate owner request.
- Preserve the primary-only path whenever the laptop or secondary runtime is
  unavailable.
- Measure progress by complete recoverable user journeys, not by another
  adapter, organ, evidence ceremony or store. One turn has one effect owner,
  one final publisher, one inherited deadline and one authorized source set.
- Secondary and Semantic Supervisor components are advisory. They may deepen
  analysis or propose a plan, but never own tools, effects or publication.
- Authorization, provenance, ingestion review, privacy, effect fencing,
  citation/coverage, cancellation, honest `UNKNOWN` and one-publication are
  product invariants; cognition profiles may never disable them.
- The owner-only autonomous Engineer Mode deliberately has no per-command HITL
  or isolated-workspace policy rail. Its hard boundary is instead fresh owner
  Telegram provenance and capability authorization; every other surface keeps
  its existing controls.
- Do not merge old feature branches wholesale. Re-audit and port only exact
  useful commits onto current `main`.
- Never overlap our full native/UI gate with SolGoodman's full gate. Check the
  active gate processes first; while his gate owns the machine, continue useful
  implementation, review or focused tests and wait for the full-gate slot.
- During implementation use focused tests plus static/change evidence. Run the
  full exact-release gate only at a clean release boundary or after a shared
  release/schema/runtime contract changes; reuse immutable commit-bound evidence
  when the certified product artifact is unchanged.
- Run every full gate from a short private `mktemp` directory under disk-backed
  `/var/tmp`, never quota-limited `/tmp`; remove that exact directory as soon as
  its evidence is captured. Keep the path short enough for Unix sockets.

## Priority order

### C1 — source correctness, containment and import-order stability

Status: implemented in this source revision; independent review, exact-release
certification and deployment remain pending. Version/schema are deliberately
unchanged: this is not another sealed `0.208.55` artifact. The release captain
must choose the next free version and follow the existing activation procedure.

- S3 charges final normalized output using the exact nested verifier JSON cost.
  The same affordable output budget is reserved and accepted. The fitting Q38
  projection remains covered, while escape-heavy output cannot consume an
  unreserved verifier budget. Repeated owned citations remain valid; unfinished
  bracketed qualifiers are rejected rather than deleted. Length-limited synthesis
  is honestly partial; a length-limited verifier cannot certify an answer.
- Research merging uses the shared public URL identity, retaining meaningful
  query parameters, scheme and non-default ports. Malformed existing sources
  remain visible to later anomaly handling without crashing the merge.
- Supervisor baseline counts malformed joined JSON and malformed assistant
  trace metadata instead of hiding rows. Duplicate JSON keys and unhashable
  task fields cannot silently pass or crash the diagnostic path. Historical
  promoted events and the 05:25Z S3 witness are not removed or recreated.
- The default Coding runner mounts only the current operation's workspace/export,
  binds a probe to one admission and directory identity, applies admitted hard
  memory/CPU limits to trusted probe/compilation, discards unbounded process
  output, and kills/reaps its supervisor on cancellation/timeout. These are not
  aggregate untrusted-process-tree guarantees. Default uploaded unittest execution
  is therefore explicitly blocked until that boundary is implemented and proved.
  Discovery finding zero tests does not claim that uploaded modules were never
  imported. Engineer Mode's separate owner-authorized boundary is unchanged.
- Orchestration contracts no longer eagerly import the complete router. This
  removes a real Coding↔orchestration import cycle; fresh-interpreter tests also
  preserve the public router exports and clean-wheel import origin.

Verification at the 2026-09-06 source checkpoint: locked Python 3.13.5 author
checks ran all 1322 affected change-tier cases: **1318 passed, 4 failed**.
The four retained native bubblewrap tests fail because `/usr/bin/bwrap` is
unavailable in this author environment; they are neither skipped nor certified.
A 135-case source regression subset also passes. Complete collection before the
two added receipt-root tests contained 26733 nodes; both new tests pass. The
Python 3.14 inventory additionally retains its two native Unicode cases.
Repository Ruff lint, the canonical 1564-file formatting scope and Mypy over
528 source files pass. These are author checks, not independent review, the
canonical hosted change gate, exact-release certification or production acceptance.
The historical receipt-root parser was updated together with the documentation:
a newer production summary cannot silently rebind old golden-journey receipts.

Next actionable work, in order:

1. Obtain native boundary results and independent review of this exact source
   range. Keep unsafe uploaded execution disabled while proving a real aggregate
   memory/CPU/process/output/disk boundary; never confuse a probe with that proof.
2. Repair Coding export/snapshot consistency: current packing writes ZIP wall-clock
   timestamps, can include compiler caches, and uses unbounded path-based reads.
   Make the existing source carrier deterministic and bounded, with one exact
   snapshot for manifest/digests/output; preserve one file versus one archive.
3. Wire real N2 answer/task evidence consumption, then a functional N3 Python
   create/modify/test/repair/persistent-revision journey, and measured N1/N4 mixed
   journeys. Existing scaffolds, observers and empty claim sets are not completion.
4. Revisit S3 minimum-tier selection and the extra-hop activation lifecycle only
   with compatibility/restart/replay tests and independent review. No gate bypass.

### P0G — canonical Gate Diet

Status: complete on `main` through implementation head
`31c48d2541ccfce1c932f63ece28536c9fc346c3`; it had no standalone activation
and is now included in production `0.207.90`.

Evidence: certifying head `60ce37191bce3fd311b617bf9b7ae3e885dda9ac`
passed 24,313 exact nodes with zero skips/retries in 473.833 seconds; summary
SHA-256 `34351e090e1aeb3a7d1ee3d0544f07c2669a819e48c7842b32845de0ff884cc6`.
The non-certifying same-wheel projection
`3da073dc93dfdbf6d8e59a3294f7c830b4e96a5a` then ran 24,315 nodes against the
exact `0.207.84` wheel `954641e3…` in 475.033 seconds at topology 20/4, versus
744.736 seconds at 12/1: 36.2% lower wall time with zero retries. Measurement
summary SHA-256 `043b473402d0f2970b2341f1af2f0d6910f4cb90a2a8a1447ba34a95fc6a94bb`.

Goal: reduce mandatory gate time, disk churn and maintenance surface without
removing any unique release-blocking invariant. Regression testing, exact
release certification and live/physical observation must no longer run as one
ever-growing ceremony.

1. Record the current executable nodeid inventory, wall time, CPU/RSS, scratch
   bytes and flaky/retry history; map every mandatory node to one named product
   invariant and one authority tier.
2. Keep a compact change gate for static, contract and unique deterministic
   integration coverage. Move wheel/install, schema, backup/restore, rollback
   and owner smoke to one exact-candidate release gate. Move physical devices,
   live network/providers, large artifacts and long fault batteries to
   nightly/on-demand gates.
3. Delete or merge duplicate tests only when another authoritative node covers
   the same failure boundary. Preserve unique adversarial, restart, privacy,
   effect-fence and fail-closed coverage.
4. Forbid permanent nested release factories, mutable-host self-attestation and
   repeated execution of the same closed inventory in the normal gate. Expensive
   one-off release rehearsals use private disk-backed scratch and are removed
   immediately.
5. Require every new gate node to declare its invariant, tier, maximum runtime
   and scratch budget. A new duplicate must replace an older node or justify its
   additional fault boundary.

Acceptance: all current release-blocking invariants remain mapped and green;
the exact-candidate release result is unchanged; before/after measurements are
published; no hidden skip or permissive fallback is introduced. Target at
least 30% lower mandatory wall time, with larger cuts accepted only when the
invariant map proves equivalent coverage.

### P0H — bounded release-artifact retention

Status: reader and pair-bearing writer deployed through `0.207.92`. Exact-release
gate passed 24,729 nodes with evidence SHA-256
`518ad092aadf2ffe1b350754bd3388df5e0a19de56322ab98ef791415659942e`.
Two independently authenticated v2 generations, durable fallback, exact scope
and privileged no-delete probe are live. Because Linux still provides no
boot-start/quiescence authority proving global open-reference absence, every
candidate remains referenced and no retention apply or deletion is authorized.
The reviewed R3 boot proposal is explicitly rejected and not merged: `/init`
does not prove first userspace on every admitted kernel, its installer has an
authenticated-source TOCTOU, its uninstall marker is not crash-atomic, and its
persistent system-wide `kernel.io_uring_disabled=2` policy violates the ordinary
installed-software contract of Engineer Mode. Any successor must be an explicit
one-shot maintenance boot/transaction which returns the machine to its ordinary
kernel profile before Friday or owner software resumes.

Goal: replace the unbounded release polygon with a small recoverable set. The
current inventory is roughly 54 GB of backup data, 29 GB of wheel-only releases
and 14 GB of legacy releases; deletion is forbidden until every retained and
retired object is classified by code-owned identity.

1. Resolve current, previous, schema-capable fallback, active/staged activation
   journals, canonical evidence roots and the newest independently verified
   database/inbox/Engineer/Obsidian backup before proposing any deletion.
2. Keep all identities needed for immediate rollback and one older verified
   disaster-recovery generation. Keep an unfinished transaction and every
   object it references regardless of age. Never infer authority from a glob,
   directory mtime or a mutable symlink alone.
3. Add a deterministic dry-run manifest with exact paths, commit/tree/wheel or
   backup receipt hashes, byte counts and one closed retention reason per
   object. Ambiguous, malformed, referenced or open objects fail closed into
   `retain`, never `delete`.
4. Delete only an exact reviewed manifest through descriptor-relative,
   no-follow traversal, then prove retained releases still authenticate and a
   production-copy restore plus immediate rollback remain possible.
5. Apply the same bounded policy to obsolete gate/release scratch while keeping
   canonical release receipts and measurements outside disposable roots.

Acceptance: production is untouched during classification; dry-run and apply
manifests agree exactly; current/previous/fallback and both retained backup
generations reauthenticate; no open or journal-referenced path is removed; disk
recovery is measured and the normal release path enforces retention thereafter.

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
closed; current continuation belongs only to S1 and further toolchain expansion
is demand-driven.

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

P1B `EngineerWorkItem v1` is live in `0.207.67`: restart-safe continuation,
independent-ledger reconciliation, code-owned dependent-command source slots,
exact Work Item/fence/job/terminal-receipt binding, bounded replan and atomic
final Work Item CAS publication are active. Durable Work Items contain no
prompt, CoT, argv, output or path bodies; `RUNNING`/`UNKNOWN` is never replayed
blindly. Backup/restore and schema-capable fallback were admitted before the
signed owner smoke.

### S0 — freeze the constructor

Status: enforced architectural ratchet; exact-release evidence defect closed in
`0.207.67`.

- Do not start another journey-specific store, generic WorkGraph, Host Control
  expansion, closed compiler catalog, MCP platform, sensitive-data plane,
  Decision store, material-quarantine path or heuristic-retirement system.
  Existing bug fixes remain allowed.
- Keep the shipped Supervisor foundation and accepted
  `gptoss20b-2335df…` profile; do not build a second release PKI, effect owner
  or post-commit observer around them.
- Keep the released exact-release validator bound to named executable tests and
  an authenticated owner Telegram smoke where an external edge exists. A
  mutable or self-declared receipt is never `VERIFIED`.
- Do not salvage stale feature branches wholesale.

### S1 — finish Engineer continuation

Status: deployed in `0.207.67`.

- Preserve the released ledger-loss/rollback, source-slot, restart,
  publication and backup/restore invariants.
- Retain the distinct schema-capable fallback and keep the focused/native gates
  plus one benign signed owner smoke release-blocking.
- Do not add a generic graph, model-authored persistence or a new approval rail.

Acceptance: 1,652 focused tests; native gate 23,021 Python plus 31 UI tests;
static gate clear; two independently built wheels were byte-identical; immutable
activation, authenticated backup boundary, schema-capable fallback and signed
private-owner command smoke all cleared.

### S2 — one turn nervous system

Status: complete. Foundation deployed in `0.207.68`, scalar runtime propagation
in `0.207.69`, and strict tenant-bound file/attachment/V12 adoption in
`0.207.70`.

- Extend or wrap the existing `orchestration.contracts.TurnInput`; do not add a
  third unrelated DTO. Build one immutable authenticated turn contract after
  ingest/admission with actor, tenant, person, conversation, mode, turn ID,
  inherited deadline/budgets, authorized source identities, turn policy,
  effect fence and exactly one pending-work owner.
- Router, legacy runtime, Engineer, secondary and Supervisor consume the same
  contract. `AgentContext` is derived from it. Fallback may change strategy but
  may not discard the plan/source identities or classify the raw message again
  from zero.
- Carry one turn ID through ingest, route, tools and final publication. Land the
  current small-model behavior first with no user-visible change.

Foundation acceptance: 86 focused and 677 expanded contract tests; native gate
23,108 Python plus 31 UI tests; static gate clear; three independent reviews
accepted; two clean wheels were byte-identical; immutable activation and the
schema-46 backup/fallback boundary cleared.

First runtime-slice acceptance: 1,081 focused/related tests; independent review
accepted 922 exact S2 tests; native gate 23,230 Python plus 31 UI tests; static
gate clear; two wheels were byte-identical. Production activation created and
verified one SQLite/inbox/Obsidian/Engineer recovery set, retained the distinct
schema-46 fallback and reached a terminal `clear` receipt. Signed bridge-owner
identity returned `200`; an isolated authenticated two-turn owner chat returned
`200/200` on one conversation. Canonical rollback/restore paths passed the
release gate; no destructive production rollback was manufactured.

Completion acceptance: 860 changed-suite and 167 focused integration tests plus
72 signed-Telegram activation tests passed; independent integration, release,
security and whole-diff reviews were clean. The canonical native gate passed
23,306 Python plus 31 UI tests with no skips, including pinned Syncthing and
native PowerShell boundaries; static checks were clear and two clean wheels
were byte-identical. Immutable activation retained the distinct schema-46
fallback, produced verified SQLite/inbox/Obsidian/Engineer recovery receipts,
and ended `clear`; signed private-owner identity returned `200`. No synthetic
attachment traffic was inserted into the live owner conversation.

### S3 — bounded second hemisphere

Status: readiness and authenticated bounded-advisor path deployed in `0.207.71`;
on-demand runtime admission refresh deployed in `0.207.82`; assist-controller
cutover deployed in `0.208.11`. Observation-bound consumed witness landed
2026-09-06T05:25Z on live `0.208.51` assist. No-product extra-hop restore
after the historical promoted row landed on live `0.208.55` assist.
Compact health after a cutover process epoch is `requested_mode=assist`,
`effective_mode=off` (`material_loaded_not_accepted`); that is
process-epoch compact, not a missing witness. Do not reopen S3.

- Keep the primary as sole tool caller, effect owner and publisher. Secondary
  and Semantic Supervisor receive the shared turn contract and return bounded
  structured advice only; their absence is exactly the primary-only path.
- Let joined shadow observations accumulate from real eligible traffic; target
  20 for a useful sample, never fabricate traffic and never make the count a
  product-availability gate.
- Promote only the current-file-plus-current-public-web journey to bounded
  assist after release-bound evidence, latency evidence and assist-to-shadow
  rollback. Keep effect planning and heuristic retirement deferred.

Acceptance: focused authenticated-turn/advisory regressions passed; the native
gate passed 23,350 Python plus 31 UI tests with zero skips; static checks were
clear and two clean wheels were byte-identical. Immutable activation retained
schema 46 and the distinct schema-capable fallback, created verified
SQLite/inbox/Obsidian/Engineer recovery receipts and ended `clear`; signed
private-owner identity returned `200`. A real consumed representative-window
witness now exists on live `0.208.51` (see Closed on production `0.208.51`).
Do not fabricate additional traffic. Assist still does not change the sealed
query, does not own synthesis, and does not expand into coding or research
as a new journey. Honest residual of that witness is `partial_evidence`
(`web_source_truncated`, `local_context_truncated`) — an audit finding,
not an S3 reopen.

The primary keeps files local and sends only an independent sealed public-web
topic. Current files (one or several), a restored prior file, and an unused
current upload no longer veto an independent public-web clause. Same-sentence
summarize without a sealable public topic stays local and skips web instead of
refusing the whole turn. File-as-query and leftover file nouns still fail
closed for the outbound query. The genuine consumed representative-window
witness exists; do not fabricate additional traffic.

Demand-refresh acceptance: every static eligibility, evidence, actor and
capability gate is evaluated before secondary traffic; only a genuine eligible
assist turn may perform one bounded content-free runtime refresh, after which
the capability snapshot, authenticated authority and root deadline are checked
again. Ineligible turns and laptop absence retain the primary-only path. The
focused union passed 393 tests; the exact native gate passed 24,154 Python plus
31 UI tests with zero skips and static checks clear. Two clean wheels were
byte-identical. Same-schema production-copy acceptance passed 35/35 checks and
rollback passed 12/12. Immutable activation retained exact `0.207.81` as both
predecessor and schema-capable fallback and ended `clear`; trusted-CA health
reports `0.207.82`/`ok`, and signed private-owner chat returned exact `OK`.

### S4 — one search facade with passage memory

Status: R0 measured recall and R1 corpus-backed lexical gap closure deployed in
`0.207.72`; the reader-first schema/coverage contract is deployed in `0.207.73`.
The bounded writer and restart-safe backfill are deployed in `0.207.74` and are
converging in production. R2c v3 passage topology and authenticated stored
locators are deployed in `0.207.76`. R3 measured search-facade parity is
deployed in `0.207.78`; R4a reader-first conversation passages are deployed in
`0.207.79`; R4b schema-50 writer/lexical activation is deployed in `0.207.80`;
R5 measured conversation recall is deployed in `0.207.83`; R6 bounded lexical
refill and R7 five-contour document recall are deployed in `0.207.93`; the R8A
compatibility/model-control slice is deployed in `0.207.95`; fail-soft dense
document passage recall is deployed in `0.207.96`; the R8B internal retrieval
foundation is deployed in `0.207.98`; R8C exact focused-source parity is
deployed in `0.207.99`; and R8D authenticated exact-message windows are
deployed in `0.208.0`. Local `main` additionally carries the accepted R8E
bitemporal/graph internal lane, the archive composite seam, a dispatch owner
that expresses exact window / `as_of` / `known_at` / graph, and a sole-facade
measurement with `cutover_ready` true. R8 sole dialogue-facade retirement is
not yet a production release.

1. Add reader-first `document_passages` with a schema-capable fallback, bounded
   writer, restart-safe resumable backfill and honest `index_incomplete`.
2. Make `archive_search` reach parity with document, promoted-knowledge,
   bitemporal/graph and message semantics. Only after regression and shadow
   recall measurements may `memory_search`, `source_search` and
   `message_search` disappear from the dialogue model catalog; they remain
   internal adapters.
3. Add conversation passages and adjacent context as a separate reversible
   release.
4. Reconstruct a current measurable recall set before changing ranking or
   embeddings. Close filename, alias, format, date and truncation holes only
   from corpus-backed failures; do not revive `raw_fts.metadata_json`.

R0/R1 acceptance: the real-path benchmark has 21 closed cases across all ten
classes, body-free deterministic reports and exact release/origin binding. The
only accepted runtime change raised measured positive recall from 14/20 to
15/20 by repairing one reproduced capped lexical miss without changing
candidate membership, passage evidence, cursor or coverage truth. The focused
archive/benchmark suite passed 1,026 tests; the native gate passed 23,512 Python
plus 31 UI tests with zero skips; static checks, two-run benchmark identity and
two byte-identical wheels cleared. Immutable activation retained schema 46 and
the distinct fallback, produced a verified recovery set and ended `clear`;
signed private-owner identity returned `200`.

R2a acceptance: the schema/reader, archive, lifecycle and migration suites
passed 399, 899, 323 and 142 tests respectively; the exact final native gate
passed 23,540 Python plus 31 UI tests with zero skips and static checks clear.
Independent schema/lifecycle and reader reviews were clean; readiness over
100k/500k stored child rows measured 0.158/0.516 ms median without rechunking.
Two wheels per sealed sibling were byte-identical. A production-copy 46→47
migration preserved all 4,475 Raw identities, created 1,991 explicit-incomplete
parents and zero children, reopened identically under both stable and rc0, and
passed integrity/FK checks. Immutable activation retained the distinct
schema-47 rc0 fallback, produced verified SQLite/inbox/Obsidian/Engineer recovery
receipts and ended `clear`; the signed private-owner smoke returned exact `OK`.

R2b acceptance: focused writer/backfill coverage passed 333 tests and three
independent reviews were clean. The final native gate passed 23,564 Python plus
31 UI tests with zero skips; static checks were clear and two sealed wheels were
byte-identical. Production-copy acceptance proved restart continuation every
three pages, idempotent replay, backup/restore identity, fallback reopen and an
honest zero-missing/zero-stale audit. Immutable activation produced verified
SQLite/inbox/Obsidian/Engineer recovery receipts and ended `clear`; trusted-CA
health reported `0.207.74`/`ok`, and the signed private-owner Telegram smoke
returned exact `OK`. The two detected sparse-text topology failures remain
explicitly incomplete rather than being falsely marked current.

R2c acceptance: 323 combined focused tests and an independent 88-test audit
passed; the six adversarial topology/dependency blockers were clean. The native
gate passed 23,795 Python plus 31 UI tests with zero skips; static checks were
clear. Both stable and rc0 were built twice byte-identically. A sealed-wheel
production-copy migration carried 1,718 exact-current parents, retained two
honest pending parents, repaired both through the bounded worker, and reopened
under stable and the distinct rc0 with integrity/FK clean; the schema-47
predecessor rejected schema 48. Immutable activation produced verified
SQLite/inbox/Obsidian/Engineer recovery receipts and ended `clear`. Production
now has 1,720 current parents, 16,359 child passages and no pending v3 backfill;
trusted-CA health reports `0.207.77`/`ok`, V12 is `canary_ready`, and the signed
private-owner smoke returned exact `OK`.

R3 acceptance: facade materialization now preserves authorized candidate tails,
conversation-first diversity and one-shot keyboard-layout repair without
changing public limits, final reauthorization, cursor or exact replay. The
focused suite passed 396 tests. Deterministic body-free evidence kept recall at
15/20 with zero false absence, matched candidate membership 6/6 and recorded the
single known order mismatch honestly at 5/6. The full native gate passed 23,945
Python plus the same 31 UI node IDs with zero skips; UI used the gate's serial
safe fallback after parallel Chromium workers hit a host SIGTRAP flake. Static
checks were clear and two wheels were byte-identical. The operator-independent
schema-48 backup verified; immutable activation ended `clear` with exact
`0.207.77` as predecessor/fallback. Trusted-CA health reports `0.207.78`/`ok`,
V12 is `canary_ready`, and the signed private-owner smoke returned exact `OK`.

R4a acceptance: schema, lifecycle, reader, privacy and performance reviews were
clean. The exact native gate passed 24,013 Python plus 31 UI tests with zero
skips; static checks were clear. Stable and rc0 were each built twice as
byte-identical wheels. Production-copy acceptance passed 17/17 checks: the
48→49 migration preserved all 1,407 conversation identities, created one
body-free `backfill_pending` projection per conversation and zero child/FTS
rows; stable and the distinct rc0 reopened schema 49, while `0.207.78` rejected
it without changing bytes. Backup/restore preserved the exact sidecar and FTS
receipt. Immutable activation produced verified SQLite/inbox/Obsidian/Engineer
recovery receipts and ended `clear`; trusted-CA health reports
`0.207.79`/`ok`, and the signed private-owner smoke returned exact `OK`.

R4b schema decision: the released schema-49 insertion/update guards authenticate
the complete prior prefix and therefore cannot provide a prefix-independent
writer bound. R4b may migrate 49→50 only to replace those guards with exact
incremental next-source/one-anchor CAS proofs and the supporting index. It may
not add an authoritative store or change R4a's public/body-free contracts. The
request path remains bounded and fails soft to complete legacy message history;
schema 49 is the fail-closed predecessor and a distinct schema-50 rc is the
fallback.

R4b acceptance: schema, writer, reader, provenance and privacy reviews were
clean. The exact native gate passed 24,128 Python plus 31 UI tests with zero
skips; static checks were clear, and stable/rc0 wheels were each reproduced
byte-identically. Production-copy acceptance passed 35/35 checks across the
49→50 migration, two reopens, all 85 authoritative tables, bounded writer and
backup/restore. The 12/12 rollback rehearsal selected the never-activated
schema-50 rc0 after an injected post-migration health failure and rejected stale
tree/backup identities before mutation. Immutable activation produced verified
SQLite/inbox/Obsidian/Engineer recovery receipts and ended `clear`; trusted-CA
health reports `0.207.80`/`ok`, and signed private-owner chat returned exact
`OK`. The bounded production backfill is active and remains honestly partial.

R5 acceptance: the exact conversation package retained a matched message in
bounded excerpts and added deterministic authenticated conversation-journey
measurement without changing storage authority. Independent review was clean;
31 focused tests, six compatibility tests and six restart/replay parameter
cases passed. The native gate passed 24,175 Python plus 31 UI tests with zero
skips; static checks were clear and two wheels were byte-identical.
Same-schema production-copy acceptance passed 35/35 checks and rollback passed
12/12. Immutable activation retained exact `0.207.82` as predecessor and
schema-capable fallback and ended `clear`; trusted-CA loopback/LAN health report
`0.207.83`/`ok`, and the signed private-owner smoke passed all 11 checks.

R6/R7 acceptance: one conditional bounded lexical refill closes both reproduced
conversation channel gaps without changing the complete-history absence
authority. The document harness measures filename, alias, MIME format, legacy
calendar date and bounded truncation through the real archive path. The focused
regression union passed 1,197 tests. The exact release gate passed 24,739 Python
plus 32 UI tests with zero failures or skips; summary SHA-256
`f584a21d4684aebb2600f2dba3a20c26870d5bb2c409534b523bfb1217cdd2c1`.
Two independent builds and a clean-unpacked-ZIP build reproduced the exact wheel.
Immutable activation ended `clear`; DR index revision 20 retains `0.207.93`
current and `0.207.92` older with retention `review_required`. Trusted-CA health
reports `0.207.93`/`ok`, and signed private-owner smoke passed all 11 checks.
The broader R8 candidate failed 60 existing routing/tool-call regressions and
was excluded before release; legacy dialogue contracts remain until real parity.

Inbox-fairness acceptance: selection now filters current-policy advice,
exhausted attempts, private dependencies and secondary-product witnesses before
the bounded worker page, then orders immutable creation identity oldest-first.
Focused worker/storage coverage passed 69 tests and the explicit tenant-scope
contract passed six. The exact release gate passed 24,739 Python plus 32 UI
tests with zero failures or skips; summary SHA-256
`81ac61657ea7f68d601fcfc4e688577e674fbfb91e188db84ef4e1b731e25ce1`.
An independent private-clone build and clean-unpacked-ZIP build reproduced the
exact wheel. Immutable activation ended `clear`; four backup surfaces were
receipted, DR index revision 24 retains `0.207.94` current and `0.207.93` older,
trusted-CA health reports `0.207.94`/`ok`, and signed private-owner smoke passed
all 11 checks.

R8A compatibility acceptance: the released legacy textual tool-call and facade
surface remains intact while malformed late-start carriers fail closed and
quoted prose plus labelled non-JSON fences cannot become executable control.
The exact release gate passed 24,758 Python plus 32 UI tests with zero failures
or skips; summary SHA-256
`b41d058539e71f58645450e17c0c96f75a54fa9aafd394bd16b6aec7e941e523`.
Independent clean-checkout and Git-archive builds reproduced wheel
`fc2b002fc742164554e2e01cfe5d0678182741f70c223fb1b6fe00e5fe8ac266`.
All four clean-artifact journey bundles are release-bound and verified under
binding `95800fe23d86e497faf83ed6d6578edaa5ffb9fbe48c4d64bffc131ea0298335`.
Immutable activation ended `clear`; DR lifecycle revision 28 retains
`0.207.95` current and `0.207.94` older with retention `review_required`.
Trusted-CA health reports `0.207.95`/`ok`, and signed private-owner smoke passed
all 11 checks.

Dense document recall acceptance: `archive_search` now admits bounded dense
passage candidates without replacing its complete lexical/message tails, then
re-authorizes and re-scores exact-current sources before returning provenance.
Missing, stale or failed dense state fails soft to the released non-dense path.
The frozen body-free 140-document/24-qrel synthetic ranking corpus improved
recall@10 from 12/24 to 24/24 and recall@20 from 13/24 to 24/24 with zero
foreign-authority results; this evidence deliberately makes no claim about the
private production corpus or production embedding-model quality. The exact
release gate passed 24,772 non-UI plus 32 UI tests with zero failures, skips or
retries; summary SHA-256
`ae22e5d4b2689bfdf927c8638b7cc1e43e009651c0261e00ae0c9a46a3fe3a88`.
Independent clean-checkout and clean Git-archive builds reproduced release wheel
`bd7645b2f9120ab3c51acbe82d83269dd8cd0d86f30af31d9b112539d221be3a`.
Immutable activation ended `clear`; DR lifecycle revision 32 retains
`0.207.96` current and `0.207.95` older with retention `review_required`.
Trusted-CA health reports `0.207.96`/`ok`, and signed private-owner smoke passed
all 11 checks.

R8B acceptance: code-owned memory, message and source retrieval now uses one
explicit internal execution scope with fresh account, preset and override
authorization; message retrieval additionally requires `conversations.read`.
Model-selected message search has a closed argument projection, and final
publication rechecks both capabilities so a late revoke produces a source-free
denial without result bodies, evidence, files, voice, citations or continuation
state. The six benchmark compatibility calls use the same internal lane. The
legacy dialogue tools remain visible because `archive_search` does not yet
match memory `as_of`/`known_at` and graph context, source focus, or message
empty-query/full-window/full-content semantics. The exact release gate passed
24,881 non-UI plus 32 UI tests with zero failures or skips; summary SHA-256
`25f85373fab4073c0e6c9704d9acec600619d472a7b33123db249ef2ca1ce3f0`.
Two independent private-clone builds reproduced exact wheel
`878ed0a100e8bfbfb56e2bb75f992e35706557b22f5525e93ef1caad910a7632`.
Immutable activation ended `clear`; DR lifecycle revision 40 retains
`0.207.98` current and `0.207.97` older with retention `review_required`.
Trusted-CA health reports `0.207.98`/`ok`; the read-only production observation
found schema 50, zero foreign-key violations and zero hard contradictions, and
the signed private-owner smoke passed all 11 checks.

R8C acceptance: query plus source focus now has one exact v2 contract across
authorized lexical and dense selection, body-free rowid lead selection with
exact authorization joins, stable replay and benchmark parity. Legacy passage
v1 remains compatible; multiline v2 is conditional, and stale or inauthentic
dense state still fails closed or soft to the released lexical path. The exact
release gate passed 25,258 non-UI plus 32 UI tests with zero failures or skips;
summary SHA-256
`4f89cbd32d8a3594714df80f124683c72779d80940981691d1d2a3dd668ea4ed`.
The private-clone and clean-unpacked-source builds reproduced wheel
`3ce9144b544d99a7c50671f7ca7a239d128b2eb8c0f0ef46c9c2f3d0fc9c6dd4`.
Immutable activation ended `clear`; DR lifecycle revision 44 retains
`0.207.99` current and `0.207.98` older with retention `review_required`.
Trusted-CA health reports `0.207.99`/`ok`; the authenticated production
read-only bundle is verified under binding
`d7c01df8270985f13a0b50932cedfa9e5e20a1e0bef5ca9ed0f0a10d20022af7`,
and the signed private-owner smoke passed all 11 checks.

R8D acceptance: queryless exact conversation windows now enter through one
authenticated code-owned lane with durable ingress bounds, stable page-chain
identity, full source reauthorization and atomic final publication. Partial
page failure retains only the accepted witnessed prefix; carrier loss, replay,
late permission drift and SQLite snapshot drift fail closed without exposing
message bodies to the dialogue tool catalog, TTS or durable Work Items. The
exact release gate passed 25,324 non-UI plus 32 UI tests with zero failures or
skips; summary SHA-256
`4194a2cf0499099ebccc47d567857fc6d92099024a75f537bcbeaf51d6dd1c2a`.
Two independent private-clone builds and a clean source-ZIP rebuild reproduced
wheel
`1354792971eb50b792c6a65ea7f86db47f0b9a643b7fb113dbbaddd733078490`.
Immutable activation ended `clear`; DR lifecycle revision 48 retains `0.208.0`
current and `0.207.99` older with retention `review_required`. Trusted-CA
health reports `0.208.0`/`ok` with the V12 gate `canary_ready`; the authenticated
production read-only bundle is verified under binding
`b02177bb427c5ff5aa537ffb7460a69aec897a8ac9eb573f5a272eb0105c68e8`,
and the signed private-owner smoke passed all 11 checks.

Estimate: 4–10 clean-work days across the remaining separately reversible releases.

### S5 — measured cognition and installation budgets

Status: R0/R1 deployed in `0.207.77`; schema remains 48.

- Extend the existing attested `V12ModelProfileSpec`/lease instead of creating a
  competing environment-only model profile. Measured capabilities control
  lexical routing aids, verifier policy, history, tool rounds/calls, prepared
  evidence, native tools and context use.
- Preserve a baseline equivalent to today's 27–35B behavior. More capable
  leases may remove cognition crutches only after probes; authorization,
  provenance, review, privacy, citation/coverage, effect fencing and
  publication invariants never vary by model.
- Separate safety deadlines from model anti-loop limits and installation
  resource budgets. Every nested stage inherits the parent deadline.
- Remove the artificial V12 8k ceiling where the exact runtime is attested for
  40k; use `min(attested runtime, installation cap)` rather than a model-name
  guess.

Acceptance: the 1,347-node focused union and the complete native gate (23,926
Python plus 31 UI tests, zero skips) passed. Exact live q38/SGLang attestation
proved verified, installation and effective capacity of `40960` tokens with the
leased capacity authoritative through planner, file, archive, document and
current-file/public-web journeys. Two deterministic wheel builds were
byte-identical. The operator-independent schema-48 backup verified, immutable
activation ended `clear`, exact `0.207.76` remains the sealed fallback, and the
signed private-owner smoke returned exact `OK`.

### S6 — journey proof and recovery, not new organs

Status: current mission/reminder and Telegram ingress/delivery recovery audit
deployed in `0.207.75`; R2 Telegram browse-to-full-document closure is deployed
in `0.207.81`; R3 exact journey evidence is deployed in `0.207.84`; R4
candidate-bound restart/fault evidence and authenticated production observation
are deployed in `0.207.97`. Physical Android acceptance remains owner-bound.

- Keep core Telegram, web, file/Office, Obsidian, Engineer and reminder paths
  release-blocking with named deterministic tests and exact-release evidence.
- Audit scheduled work for at-most-once delivery, restart/cancel/expiry and
  uncertain-effect recovery. Change the design only where a current journey is
  actually broken.
- Prove release-level clean-home backup/restore, deterministic index rebuild,
  ENOSPC, clock skew and duplicate inbound behavior without duplicating generic
  operator evidence into every journey row.
- Re-measure review queues before adding bounded aging/observability. Keep
  export-before-purge deferred until a real retention journey requires it.
- Close the reproduced Telegram `/browse` and profile-document dead end by
  reusing the existing tenant-gated `doc:show` callback path. Preserve empty or
  malformed-result behavior and do not add another document API or store.
- Perform Obsidian Android round-trip, reconnect and conflict acceptance only
  with the owner/device present; server-side work is otherwise complete and the
  companion remains excluded.

R0/R1 acceptance: mission execution now has exact-attempt claims, completion
fences and compensation CAS; reminder payloads cross only a reauthorized
send-edge claim and ambiguous acceptance is never replayed; scans are bounded
and clock skew cannot wedge recovery. Telegram poll admission, callbacks,
commands, cached answers, chunks and terminal notices converge across commit
faults, restart, duplicate pages and ENOSPC without duplicating an admitted
effect or answer. The final native gate passed 23,774 Python plus 31 UI tests
with no skips; static checks were clear and two sealed wheels were byte-identical.
Immutable activation retained schema 47 and the distinct schema-capable
fallback, created verified SQLite/inbox/Obsidian/Engineer recovery receipts and
ended `clear`. A copy-only rehearsal rehashed the complete recovery set and
reopened its main/inbox stores twice under both candidate and fallback with
schema 47 and clean integrity/FK checks. Trusted-CA health reported
`0.207.75`/`ok`, and the signed private-owner chat returned exact `OK`.

R2 acceptance: every eligible browse, tag, entity, namesake and profile-document
result now reaches the existing tenant-gated full-document callback, and every
derived callback remains within Telegram's 64-byte limit; malformed or
overlong identities are omitted honestly. The focused suite passed 154 tests,
and the exact native gate passed 24,137 Python plus 31 UI tests with zero skips;
static checks were clear and the wheel build was deterministic. Same-schema
production-copy acceptance passed 35/35 logical/schema checks, rollback passed
12/12, and all private disk-backed temporary trees were removed. Immutable
activation retained schema 50 and exact `0.207.80` as predecessor/fallback,
ended `clear`, and produced verified SQLite/inbox/Obsidian/Engineer recovery
receipts. Trusted-CA health reports `0.207.81`/`ok`; signed private-owner chat
returned exact `OK`.

R3 acceptance: four exact clean-artifact journeys — conversation recall,
document recall and answer, durable scheduled work and honest degradation —
have deterministic privacy-safe manifests and receipts bound to the deployed
commit, tree, wheel, schema and closed executable node IDs. The focused union
passed 131 tests; the native gate passed 24,279 non-UI plus 31 UI tests with no
skips and all static checks clear. Two wheels were byte-identical;
production-copy acceptance passed 35/35 and rollback rehearsal passed 12/12.
Immutable activation retained exact `0.207.83` as predecessor and
schema-capable fallback, ended `clear`, and both trusted-CA health and the
11-check signed private-owner smoke passed on `0.207.84`.

R4 acceptance: the exact release gate passed 24,871 non-UI plus 32 UI tests
with static checks clear; summary SHA-256
`44486c6fc358d821376e80aec5f5942d7a0dedb16c9ce1bc3bbddeba536e278b`.
Two independent gate-profile builds reproduced wheel
`a0925c0399637499b2eb6b027ff495f173b618ed1b378950781b5e551fc97e62`.
Six current candidate-bound clean/restart bundles and one externally
authenticated production read-only bundle are verified under release binding
`f7a7ab8311b413d9beb6b349144a589d560b5a49c61c0892ebbb333c5284042b`;
their public evidence contains no private observation path or body. Immutable
activation ended `clear`; the DR lifecycle published revision 36 with
`0.207.97` current, `0.207.96` older and retention `review_required`. Trusted-CA
health reports `0.207.97`/`ok`, and signed private-owner smoke passed all 11
checks.

### N0 — Post-backlog baseline reconciliation

Status: live identity reconciled 2026-09-04. Production, `main` and the
sealed sibling were `0.208.11` / `6b61987a` at that reconciliation. S3
assist-controller consume is done. The observation-bound promotion
witness closed on live `0.208.51` (2026-09-06T05:25Z). Assist restored
on live `0.208.55`. N0 does not
block N1.

- [x] Reconcile source candidate, production, fallback, journals and evidence.
- [x] Deploy or explicitly supersede the `0.208.2`–`0.208.10` candidate chain.
- [x] Record exact live identity in this register (`0.208.11`, schema 50,
      predecessor `0.208.10`, activation `clear`).
- [x] S3 advice on a genuine eligible turn — observation-bound consumed
      witness on live `0.208.51` assist, 2026-09-06T05:25Z. Independent
      classification, not owner appearance. Do not fabricate traffic.

### N1 — Universal Operation Progress and Two-Message UX

Status: contract live on `0.208.18`. Ordinary `/chat` status, Engineer
Telegram status, file-album DOCUMENT status, Engineer FILE/ARCHIVE
carrier, observed web/archive chat status after `/api/chat`,
restart/edit-reject/send-fence/cancel Telegram method counts, and one
final carrier packing are live. Backend wait stays CHAT and does not
mint SEARCHING_SOURCES. Mandatory owner contract: one user message →
one editable Friday status → one final result carrier. Reuse
`TelegramStatusMessageManager`. Do not add a second execution engine.

- [x] Shared `OperationProgressProjectionV1` and code-owned Russian renderer
      (`friday/orchestration/operation_progress.py`). Truthful measured
      progress only; at most one current-focus step; no fabricated percent
      or ETA.
- [x] Migrate ordinary `/chat`, files, archive and web onto that projection
      with one status created at the start of an interactive operation.
      Ordinary `/chat` status rendering is on the projection; file-album
      status uses the DOCUMENT files projection. After `/api/chat` returns,
      observed web sources use FORMULATING_ANSWER and generated
      archives/files use archive or DOCUMENT delivering. Backend wait
      stays CHAT. Live on `0.208.18`.
- [x] Migrate `/engineer` user carrier: policy lives in
      `friday/orchestration/engineer_result_carrier.py`; Telegram publication
      sends TEXT, one ordinary FILE, or a user ARCHIVE without receipts/logs.
      Status() diagnostic ZIP is unchanged. Live on `0.208.18`.
- [x] Migrate `/engineer` Telegram status: coalesces to one editable
      message and uses the shared renderer. Live on `0.208.18`.
- [x] One final carrier: text, one file with caption, or one deterministic
      archive. Chat `make_file` packing (one file or one ZIP) is live on
      `0.208.18` (`friday/orchestration/operation_result_carrier.py`).
      Delivery uncertainty edits status and never duplicates.
- [x] Restart, edit-reject, send-fence and cancel/timeout/`UNKNOWN` proof
      with actual Telegram message counts. Live on `0.208.18`.

### N2 — Deep Web Research and Automatic Knowledge-Gap Search

Status: research I/O/observers are reported live on `0.208.26`; N2 product
completion is open. `_web_research` plans a 2–8 complementary
public-query mission, executes those queries on fact-bearing public
sources, and observes remaining N2 gates on the already-admitted
report. Empty research does not attach observation keys.
`SEARCH_BLOCKED_PRIVATE` never becomes a query plan. The observer
does not search, fetch, or invent claims. Live `_web_research` still
refuses private observed URLs, empty-after-outbound
(`no_admitted_sources`) and invalid provider facts. `_web_search`
and `_web_fetch` refuse `BLOCKED_PRIVATE`. File+web comparison and
`POST /api/ingest/url` consume `BLOCKED_PRIVATE`. Private N2
self-score is Friday's own gates, not Gemini parity. Do not claim
Gemini parity without a paired scored set. Do not fabricate
witnesses.

The checkboxes below acknowledge those specific contracts/observers only.
- [ ] The requesting answer or task consumes an exact admitted evidence bundle.
      Validate claims/citations from the actual result, not an empty claim set;
      unsupported/current-sensitive/contradictory evidence must affect publication.
- [ ] Automatic currentness and configured-provider fallback reach a real consumer,
      without private queries or new provider credentials.

- [x] Automatic currentness / knowledge-gap policy module on `origin/main`
      (`friday/orchestration/web_currentness_policy.py`). Not wired into
      live `web_surfer` or Telegram. Private filenames, paths and deictics
      stay local.
- [x] Multi-query research mission planner live on `0.208.26`
      (`friday/orchestration/web_research_mission.py`). Emits 2–8
      complementary public queries; `SEARCH_BLOCKED_PRIVATE` never becomes a
      query plan. Kernel `_web_research` plans and executes complementary
      queries on fact-bearing public sources.
- [x] Provider selection and honest degraded-fallback policy on
      `origin/main` (`friday/orchestration/web_provider_policy.py`). Not
      wired into live `web_surfer`. PRIMARY_OK / FALLBACK_USED /
      DEGRADED_PARTIAL / UNAVAILABLE; empty success is refused.
- [x] `WebEvidenceBundleV1` contract on `origin/main`
      (`friday/orchestration/web_evidence_bundle.py`). Not consumed by the
      requesting workflow yet.
- [x] Public-web source diversity on `origin/main`
      (`friday/orchestration/web_source_diversity.py`). Lexical hostname
      only; EMPTY / SINGLE_HOST / CONCENTRATED / DIVERSE. Observed live
      on `0.208.26`.
- [x] Research consumption gate on `origin/main`
      (`friday/orchestration/web_research_consumption.py`). CONSUMABLE /
      CONSUMABLE_DEGRADED / BLOCKED_PRIVATE / UNAVAILABLE. Live kernel
      `_web_research`/`_web_search`/`_web_fetch`, file+web comparison and
      `POST /api/ingest/url` refuse `BLOCKED_PRIVATE`; kernel research also
      refuses empty-after-outbound. Other states are not yet consumed
      by the requesting workflow.
- [x] Research readiness composition observed live on `0.208.26`
      (`friday/orchestration/web_research_readiness.py`). READY /
      READY_DEGRADED / NOT_READY from mission, diversity and consumption.
- [x] Citation host coverage on `origin/main`
      (`friday/orchestration/web_citation_coverage.py`). COMPLETE / PARTIAL /
      EMPTY / BLOCKED_PRIVATE; lexical hostname only. Observed live
      on `0.208.26`.
- [x] Claim-support contract on `origin/main`
      (`friday/orchestration/web_claim_support.py`). COMPLETE / PARTIAL /
      EMPTY / UNSUPPORTED / BLOCKED. Contradicting-only and unknown source
      ids are not support. Observed live on `0.208.26`.
- [x] Answer-admission gate observed live on `0.208.26`
      (`friday/orchestration/web_research_answer_gate.py`). ADMITTED /
      ADMITTED_DEGRADED / HOLD / BLOCKED from readiness and citation
      coverage. Does not invent claims.
- [x] Contradiction coverage on `origin/main`
      (`friday/orchestration/web_contradiction_coverage.py`). EMPTY / NONE /
      PRESENT / UNIVERSAL / BLOCKED. Supporting-only is not contradiction.
      Observed live on `0.208.26`.
- [x] Exact mission coverage observed live on `0.208.26`
      (`friday/orchestration/web_mission_coverage.py`). COMPLETE / PARTIAL /
      EMPTY / BLOCKED. Extra executed queries do not complete coverage.
      Coverage input is only planned queries that actually ran.
- [x] Evidence grounding on `origin/main`
      (`friday/orchestration/web_evidence_grounding.py`). EMPTY / GROUNDED /
      PARTIAL / UNGROUNDED / BLOCKED. A claim is grounded iff an admitted
      supporting or contradicting source id is present. Observed live
      on `0.208.26`.
- [x] Source date coverage on `origin/main`
      (`friday/orchestration/web_source_date_coverage.py`). EMPTY / DATED /
      PARTIAL / UNDATED / BLOCKED. `retrieved_at` alone is not dating.
      Observed live on `0.208.26`.
- [x] Claim currentness admission on `origin/main`
      (`friday/orchestration/web_claim_currentness.py`). EMPTY / ADMITTED /
      HOLD / BLOCKED. `SEARCH_NOT_REQUIRED` does not admit current-sensitive
      claims. Observed live on `0.208.26`.
- [x] Passage-reference coverage on `origin/main`
      (`friday/orchestration/web_passage_reference_coverage.py`). EMPTY /
      REFERENCED / PARTIAL / BARE / BLOCKED. Title, digest and `retrieved_at`
      are not references. Observed live on `0.208.26`.
- [x] Remaining N2 contracts observed live on `0.208.26` by
      `friday/execution_kernel/web_research_gates.py` on an already-admitted
      research report. Compact keys only; fail-closed on TypeError/ValueError.
      Do not fabricate witnesses.
- [x] Private representative N2 self-score on `0.208.26`
      (`test_n2_private_self_score_is_friday_gates_not_gemini_parity`).
      Body-free compact dict of Friday's own gates. Explicitly not Gemini
      parity.
- [ ] Gemini parity needs a paired scored set. Do not claim it.
      Owner-parked into Pandora 2026-09-05 (P0W cancelled; owner will not
      enable or pay a Google billing account; Gemini API Search grounding
      unavailable). Evidence:
      `handoffs/SolGoodman/P0W-CANCELLED-BY-USER-001.md`. Not `[x]`.

### N3 — Coding Mode

Status: N3 product completion remains open. Previously deployed `/coding`
static inspect, isolated-worker boundary, admitted
archive extract, isolated untrusted `py_compile` / unittest,
prompt-to-small-project, one-final `friday-source.zip` pack, digest
and overwrite observation on extract, Coding Mode
view/plan-gate/carrier, and upload-modification EMPTY observer remain
live through `0.208.28`. Owner private Telegram only; execute/run of
uploaded programs stay fail-closed. Engineer bubblewrap is not a
Coding Worker. This is not a safety certification.
Upload-modification apply never rewrites uploaded project files. Do
not claim safe build/test of untrusted uploads.

- [x] Bare-source inspection contracts on `origin/main`
      (`coding_source_member.py`, `coding_source_tree.py`,
      `coding_source_inspect.py`, `coding_inspect_hazards.py`,
      `coding_toolchain_hint.py`, `coding_inspect_report.py`).
      EMPTY / MAPPED|INSPECTED|HINTED / BLOCKED. No execute, no
      rebuild, no file I/O, filename-suffix hints only. Live as
      `/coding` static inspect on `0.208.19`. Isolated-worker boundary
      live on `0.208.20`.
- [x] Prompt-to-scaffold groundwork reported live on `0.208.26`
      (`friday/organs/coding/create.py`). This writes a fixed no-op template,
      not an implementation of the requested application. No execute.
- [ ] Functional application creation with an independent behavior oracle.
- [ ] Real edits to an immutable uploaded-project revision, bounded tests/repair,
      durable accepted revision and exact follow-up/rollback. The existing
      `applied=False` modification observer does not satisfy this requirement.
- [ ] Aggregate resource enforcement and native acceptance for uploaded tests.
      C1 deliberately blocks the default TEST path until then.
- [x] Upload-modification EMPTY observer live on `0.208.27`
      (`friday/organs/coding/modify.py`). Composes
      `coding_upload_modification_admission` from inspect, isolation,
      identity and edit-plan facts. Never applies file rewrites of
      uploaded projects. Execute/run stay fail-closed.
- [x] Persistent project identity contract on `origin/main`
      (`friday/orchestration/coding_project_identity.py`). EMPTY /
      IDENTIFIED / BLOCKED. Exact revision only; `latest`/`HEAD`/
      `newest`/`current` fail closed. Not wired to git or a worker.
- [x] Isolated coding worker boundary live on `0.208.20`: dedicated
      Coding bwrap profile, no host secrets, no Docker socket, no
      production database, bounded network. Spawn only after
      admission; probe-only, never executes uploaded project code.
      Isolated untrusted build/test of extracted uploads is a second
      bwrap live on `0.208.24`. Execute/run of uploaded programs stay
      fail-closed.
- [x] Safe archive extract admission on `origin/main`
      (`friday/orchestration/coding_archive_extract_admission.py`).
      EMPTY / ADMITTED / BLOCKED from member metadata. Traversal,
      absolute paths, symlink/hardlink, device, size, bomb ratio,
      file count, nesting and case-fold collisions fail closed.
      In-memory zip extract observer consumes this gate live on
      `0.208.22`; host paths are never opened.
- [x] Archive extract-plan family on `origin/main`
      (`coding_archive_member_catalog.py`,
      `coding_archive_extract_plan.py`,
      `coding_archive_digest_facts.py`,
      `coding_project_isolation_admission.py`,
      `coding_archive_overwrite_plan.py`). Catalog, relative
      destinations, project-root isolation are consumed by the
      extract observer live on `0.208.22`. Digest facts and
      overwrite/collision are observed on extract live on `0.208.26`.
      Untrusted execute remains fail-closed.
- [x] One final source archive and restart/rollback contract groundwork.
      TEXT/FILE/ARCHIVE plan, manifest, pack admission, publication,
      restart, rollback and uncertainty contracts are on `origin/main`
      (`coding_result_archive_plan.py`,
      `coding_result_archive_manifest.py`,
      `coding_result_archive_pack_admission.py`,
      `coding_result_publication_admission.py`,
      `coding_result_restart_admission.py`,
      `coding_result_rollback_admission.py`,
      `coding_result_uncertainty.py`). One-final `friday-source.zip`
      pack, restart/rollback EMPTY observation and uncertainty are
      live on `0.208.26`. Never restart/rollback I/O. Execute/run
      stay fail-closed.
- [x] Coding Mode situation composition on `origin/main`
      (`coding_mode_intent.py`, `coding_mode_snapshot.py`,
      `coding_mode_execute_claim.py`, `coding_mode_plan_gate.py`,
      `coding_mode_carrier.py`, `coding_mode_view.py`). EMPTY /
      PROJECTED / BLOCKED from landed inspect/admission facts.
      Untrusted execute without an admitted worker fails closed.
      Execute-claim is composed by `/coding` static_turn live on
      `0.208.22`; isolated BUILD/TEST loop is live on `0.208.24`;
      plan-gate, carrier and view are composed live on `0.208.26`;
      the process worker remains probe-only; execute/run stay
      fail-closed.

Do not prebuild a compiler catalogue. Do not weaken Engineer Mode or
primary release certification to create the worker.

### N4 — Whole-Organism Coherence

Status: mixed-journey projections are reported live on `0.208.21`; complete
N4 execution/consumption acceptance remains open. Durable organs, the shared view, the
store-shaped projection and the store observer derive mixed journeys
from already-durable identities. Telegram MIXED is PROJECTED-only.
Primary and secondary share one operation identity. New modes compose
existing primitives. Mixed-journey is not a registered organ.

- [ ] A real file + authorized archive + public web task produces a grounded
      comparison/table through the existing runtime and one final publisher.
- [ ] Prove actual status/final transport, cancellation, restart, duplicate input
      and uncertain-send behavior for that composed task, not just its projection.

- [x] Read-only `SharedOperationViewV1` / `AgentSituationProjectionV1`
      on `origin/main` from already-supplied facts. No new execution
      owner.
- [x] Mixed-journey view contracts on `origin/main`
      (`mixed_journey_identity.py`, `mixed_journey_organs.py`,
      `mixed_journey_coverage.py`, `mixed_journey_revoke.py`,
      `mixed_journey_restart.py`, `mixed_journey_view.py`). EMPTY /
      PROJECTED / BLOCKED from already-supplied facts.
- [x] Mixed-journey store-shaped projection on `origin/main`
      (`mixed_journey_file_facts.py`,
      `mixed_journey_archive_facts.py`,
      `mixed_journey_conversation_facts.py`,
      `mixed_journey_web_facts.py`,
      `mixed_journey_table_facts.py`,
      `mixed_journey_store_projection.py`). Maps already-supplied
      organ facts into MixedJourneyViewV1.
- [x] Mixed journeys live on `0.208.21`: file+archive+conversation+web+table
      identities project through `friday/organs/mixed_journey/observe.py`;
      Telegram MIXED when PROJECTED (`admit.py`,
      `mixed_journey_progress.py`, `_status.py`). Exclusive
      DOCUMENT/ARCHIVE/RESEARCH remain otherwise. Inbound album keeps
      DOCUMENT. Filenames without ids stay EMPTY. Private
      paths/filenames BLOCK without payload. Restart, revoke-before-publish
      and secondary-absent primary-only are observed, not invented.
- [x] One turn, one operation, one status, one result, one effect owner,
      one publisher. Honest `UNKNOWN`. Primary-only when secondary is absent.

### N5 — Maintainability Ratchet

Status: standing rule, not a rewrite. `friday/agent_runtime/__init__.py` is
~76k lines / 3.81 MiB. No new product logic in that module unless no narrow
seam exists. Extract only a touched seam with exact parity tests. Do not
begin a clean-architecture rewrite. Kernel web-consumption helpers now live
in `friday/execution_kernel/web_consumption.py`; `_web_search`/`_web_fetch`/
`_web_research` still own quota and adapter I/O. Inventory of live
`0.208.24` N3 loop vs `0.208.23` found no dump into `agent_runtime`
(thin `handle_coding_static_turn` dispatch only). The implementable
checkbox is closed on `0.208.25`; this chapter stays in force.

### Removed from the active queue

- Supervisor effect observation and heuristic retirement, Package 6 trusted
  attestation-root construction, closed compiler profiles, indexed Ghidra as a
  product, generic Active Frames/WorkGraphs, Executive unification, Host
  Capability growth, MCP generalization, sensitive-data/Decision stores and a
  material-quarantine release path have no current end-to-end product gap.
- Engineer's owner shell is the toolchain. Add an installed compiler, analyzer
  or decomposer only when a real request proves it absent; do not rebuild an
  allowlisted compiler zoo around arbitrary command execution.
- Existing dormant code is not authority to resume these items. Re-entry needs
  a current owner request or a corpus-backed broken journey.

## Explicitly deferred or closed

- Obsidian companion, shared/multi-device vault, desktop control and remote
  agents: deferred.
- Local coding-agent orchestrator: development tooling, not current Friday
  product convergence.
- Old WIP blobs, old V12 model-first branch and PLAN-002/004: superseded.
- Broad exploit validation: no work without separate scope and safe target.
- Owner-parked 2026-09-03 (Pandora; not the live queue). Re-entry needs an
  explicit owner request. Named evidence is still required before any of these
  can be marked complete:
  - P0H reviewed bounded deletion — one-shot maintenance authority; no
    apply/delete while Linux cannot prove global open-reference absence.
  - S6 physical Android / Obsidian round-trip — owner and device. Do not
    touch the companion plugin without a separate request.
  - Off-machine backup/file mirror — implementation present, target empty.
  - External web-search credential rotation at the provider, then the single
    protected runtime secret.
  - Parked Sol R8I worktree/patch under `~/.jericho/runtime/friday-s4-r8i-exact-runtime.worktree`
    and `~/.jericho/grok-takeover/patches/s4-r8i-exact-runtime.unstaged.diff`.
    Do not merge it; it fights the landed dispatch owner.
  - Gemini parity / P0W Gemini-first web search — cancelled before
    implementation. Owner will not enable or pay a Google billing account.
    Keep Friday's Yandex path. Do not claim Gemini parity. Do not resurrect
    P0W. Evidence: `handoffs/SolGoodman/P0W-CANCELLED-BY-USER-001.md`.

## Owner/external actions

- S3 consumed witness is closed. Live assist is restored on
  `0.208.55`. Remaining owner-end is the Pandora box (P0H
  apply/delete, Android/Obsidian device, off-machine backup,
  provider-credential rotation). Gemini parity stays parked inside
  Pandora, visible as parked, not `[x]`.

## Standing lead-architect checklist

This is the live remaining-work register. Owner/external and observation-bound
rows stay open until their named evidence exists. Do not invent a second
checklist elsewhere. The Ctrl+T view is a compact projection of this list.

### Closed on production `0.208.11`

- [x] P0G Gate Diet
- [x] P0 production message stability
- [x] P1 Engineer Mode complete user workflow, including P1B `EngineerWorkItem v1`
- [x] S0 constructor freeze
- [x] S1 Engineer continuation
- [x] S2 one-turn nervous system
- [x] S4-R0..R8F sole `archive_search` facade, exact windows, bitemporal/graph
- [x] S4-R8I unique remainder: generic continuation cannot start a fresh
      exact selector (on `main` since `a914944f`, live since `0.208.11`)
- [x] S5 measured cognition and exact 40k lease
- [x] S6-R0..R4 mission/reminder/Telegram recovery, browse-to-document, journey evidence
- [x] S3 assist-controller cutover on `0.208.11` (`requested=assist`,
      `effective=off` until the consumed witness; witness later closed
      on live `0.208.51`)
- [x] N0 identity reconciliation: `main` = production = `6b61987a` /
      `0.208.11`; candidate chain `0.208.2`–`0.208.10` superseded

### Closed on production `0.208.18`

- [x] N1 two-message Telegram UX: `/chat`, Engineer, file-album,
      FILE/ARCHIVE carrier, observed web/archive status and one-final-carrier
      packing live on `0.208.18`

### Closed on production `0.208.19`

- [x] N3 `/coding` static inspect live on `0.208.19` (owner-private
      Telegram; execute fail-closed). Isolated worker remains the
      open N3 MVP checkbox.

### Closed on production `0.208.20`

- [x] N3 isolated `/coding` worker boundary live on `0.208.20`
      (dedicated bwrap probe, no Docker, no host secrets, no prod
      DB). Untrusted execute remains fail-closed. N3 MVP
      implement/build/test loop of untrusted uploads remains open.

### Closed on production `0.208.21`

- [x] N4 store-backed mixed journeys live on `0.208.21` (Telegram
      MIXED when PROJECTED; exclusive DOCUMENT/ARCHIVE/RESEARCH
      otherwise). One turn / one operation / one status / one result /
      one effect owner / one publisher; honest `UNKNOWN`; primary-only
      when secondary is absent. Mixed-journey is not a registered organ.

### Closed on production `0.208.22`

- [x] N3 admitted in-memory archive extract into the isolated
      `/coding` workspace live on `0.208.22` (catalog + extract
      admission + extract plan + project-root isolation; host paths
      never opened; `ZipFile.extract` unused). Untrusted execute
      remains fail-closed. Extract-only does not close the MVP
      implement/build/test loop of untrusted uploads.

### Closed on production `0.208.23`

- [x] No-product `semantic_supervisor_shadow_to_assist` restore after
      live `0.208.22`. Trusted-CA health is `0.208.23` with
      `requested_mode=assist` and `effective_mode=off`
      (`material_loaded_not_accepted`). N3 extract remains live.
      Untrusted execute remains fail-closed. Next product sibling is
      `assist_to_shadow`.

### Closed on production `0.208.24`

- [x] N3 isolated untrusted build/test loop of extracted uploads live
      on `0.208.24` (`py_compile` and stdlib `unittest.discover` of
      `test*.py` inside a second bwrap after worker admission and a
      confirmed isolation probe). Execute/run of uploaded programs
      stay fail-closed. This is not a safety certification. Trusted-CA
      health is `0.208.24` with `requested_mode=shadow` and
      `effective_mode=shadow` (`default_off`). Next product sibling
      that changes friday sources is `shadow_to_assist`.

### Closed on production `0.208.25`

- [x] No-product `semantic_supervisor_shadow_to_assist` restore after
      live `0.208.24`. Trusted-CA health is `0.208.25` with
      `requested_mode=assist` and `effective_mode=off`
      (`material_loaded_not_accepted`). N3 isolated build/test loop
      remains live and did not dump into `friday/agent_runtime`.
      Execute/run of uploaded programs stay fail-closed. Next product
      sibling that changes friday sources is `assist_to_shadow`.
- [x] N5 implementable seam extract: no giant-runtime dump from the
      `0.208.24` N3 loop (two thin `handle_coding_static_turn`
      dispatch sites). Kernel web-consumption helpers remain on
      `0.208.18` in `friday/execution_kernel/web_consumption.py`.
      The N5 chapter stays a standing ratchet.

### Closed on production `0.208.26`

- [x] N2 live research mission/gates and N3 `/coding` create, digest/
      overwrite extract observation, one-final `friday-source.zip`
      pack, restart/rollback EMPTY observation, and Coding Mode
      view/plan-gate/carrier live on `0.208.26` at
      `9316a5e6cc2e16992a4eafaab04f3fc9f66ff01a` (tree-file
      `4368e94d92a1bb5b41e81f98706fa6e1338b9a5e6c19f654efc9f14c0be8607e`,
      wheel `f98055784f1948335da9cf332b15596936a693f47b506eefc8026e8ea5c45915`).
      Trusted-CA health is `0.208.26` with `requested_mode=shadow` and
      `effective_mode=shadow` (`default_off`). Secondary after cutover
      restart: `mode=assist`, `state=cooldown`, `available=false`.
      DR published index_revision 152; retention remains
      `review_required`. Upload-modification apply stays source-only.
      Execute/run of uploaded programs stay fail-closed. Gemini parity
      is not claimed. Do not re-activate `9316a5e6`. Next product
      sibling that changes friday sources is `shadow_to_assist`.

### Closed on production `0.208.27`

- [x] N3 upload-modification EMPTY observer live on `0.208.27` at
      `e5a25c4a9b988f5f4959b3c69eca4358a0bb6770` (tree-file
      `60ea6f01ff06233e4a7965daa792d0f7fef1b16c916b22de1ed191522f84494e`,
      wheel `9e9731acd8ba5c9e3e8657d78a01baf9f806f81ee6108bddf07ae91c4d1e109d`).
      Trusted-CA health is `0.208.27` with `requested_mode=assist` and
      `effective_mode=off` (`material_loaded_not_accepted`;
      `source_revision_loaded` and `representative_window_verified`
      true). Secondary after cutover: `mode=assist`, `state=healthy`,
      `available=false` until the process-epoch health window is
      fresh. DR published index_revision 156; retention remains
      `review_required`. Apply never rewrites uploaded project files.
      Execute/run of uploaded programs stay fail-closed. Gemini
      parity is not claimed. Do not re-activate `e5a25c4a`. Next
      product sibling that changes friday sources is
      `assist_to_shadow`. Public `/api/health` `cooldown` is the
      in-process circuit, not laptop Docker liveness; laptop-local
      generation does not refresh Friday's process-epoch window.

### Closed on production `0.208.28`

- [x] Delayed secondary startup re-probe after cooldown live on
      `0.208.28` at `71833028e735e94b9020a1aa2a92c97ca141f0bf`
      (tree-file
      `ec18064695bd5116c052539907721e7e8c188535c26feb8c23575d36479a38d9`,
      wheel
      `f81cce01960bf1508eaa673e3076469197e0f479cd5cf5db9506f15c041c98fe`).
      One detached `_startup_probe` still; after the first epoch probe
      if the circuit is `COOLDOWN` it sleeps `cooldown_retry_after_sec`
      and retries inventory/canary once. Trusted-CA health is
      `0.208.28` with `requested_mode=shadow` and
      `effective_mode=shadow` (`default_off`). Secondary after cutover
      restart: `mode=assist`, `state=cooldown`, `available=false`.
      DR published index_revision 160; retention remains
      `review_required`. Execute/run of uploaded programs stay
      fail-closed. Gemini parity is not claimed. Do not re-activate
      `71833028`. Next product sibling that changes friday sources is
      `shadow_to_assist`. Compact `/api/health` `cooldown` remains the
      in-process circuit, not laptop Docker liveness.

### Closed on production `0.208.51`

- [x] S3 Supervisor advice on a genuine eligible current-file-plus-public-web
      Telegram turn. Independent classification of live `0.208.51` assist
      2026-09-06T05:25Z: user `msg_d145ce5796b54ae1`, assistant
      `msg_df97d09e4d2b4a2f` (2452 chars, not the canned 58-char
      `step_failed`), graph `graph_866d37c2794dcc1a`. Canonical file
      sha256 `d8b64d6c1152058e715d9d0f13f7e16f5c0887c4bd506f9d223697c98a93f42b`,
      caption 73 chars / 134 UTF-8. `answer_mode=semantic_supervisor_assist`,
      plan `82d3ddce1177e39c43f40f70bb07ab4fcd891f8af5641653bca9c65ecf291f8b`
      admitted, `read_current_file` complete+verified,
      `read_current_web` complete+verified, `primary_synthesis`
      partial+verified, graph `partial_evidence`
      (`web_source_truncated`, `local_context_truncated`).
      `verified=true`, `verification.status=passed`, `score=1.0`,
      citations `[F1] [W1] [W2] [W3]`. Controller compact health:
      `invoked_total=1`, `publication_total=1`,
      `event_success_total=1`, `last_promotion_reason=admitted`,
      `fallback_total=0`. Owner appearance is not this close.
      Live identity `1b01a59ceb59180abbff9ab4cc48453a770db652`
      (tree-file
      `10f4be3b6d5d1667ef09ded4b7ebb56f89c26552e26e9e11e93fab767cf5bd6d`,
      wheel
      `426695fc04df01870e55f88d7a08aff6b0f34d8dac8f1b7cbeb165823db9cf5b`),
      predecessor `0.208.50` /
      `705dbe20a664a449f00e23c45ab25921e82b607c`, journal `clear`,
      DR index_revision 252. Do not re-activate `1b01a59c` or
      `705dbe20`. Next product sibling that changes friday sources is
      `assist_to_shadow`. Honest residual `partial_evidence` stays an
      audit finding, not an S3 reopen.

Decision chain of this attempt (body-free; this is the success-path
record the owner required in the audit commit, not a failure dump):

1. `0.208.47` 02:54Z (`msg_0f4fe2d233b44903` /
   `msg_dcae55874ecc4762`): `plan_not_admitted`,
   `answer_mode=general_conversation`. GPT-OSS invented a
   length-legal `manifest_id`; parser/policy fail-closed.
2. Pin owned `manifest_id` + `budget_sha256` as schema enum.
   Product `0.208.48` (`f7e74e9a` / dense `34b3cdbc`); extra-hop
   `0.208.49` (`86f94cc5`).
3. `0.208.49` 04:06Z (`msg_889d6a8978dc4da0` /
   `msg_ad5db81a23e745a5` / `graph_65a66eeeffd06bf7`): plan
   admitted, file+web complete+verified, `_validate_answer`
   `_AnswerRejected(code=citation_labels)` after a 506-token
   `stop`, canned 58-char `step_failed`.
4. Local repro of that reject class. Product `0.208.50`
   (`ce6b0a49` / dense `705dbe20`, `assist_to_shadow`); extra-hop
   `0.208.51` (`1b01a59c`, `shadow_to_assist`).
5. `0.208.51` 05:25Z consume witness above. Independent
   classification, not owner appearance. S3 `[x]`. Gemini stays
   parked in Pandora. Remaining owner-end: Pandora.

### Closed on production `0.208.55`

- [x] No-product `semantic_supervisor_shadow_to_assist` restore after
      live `0.208.54` keep-shadow. Trusted-CA health is `0.208.55` /
      `ok` with `requested_mode=assist` and `effective_mode=off`
      (`material_loaded_not_accepted`). Live identity
      `e60860eaa89827bc5fc58d7c4f1c47514caaf2d4` (tree-file
      `706aee22e4b1a435e6ae4969d41936f603cd9863ac1e2c2535e1cfe878bb80b7`,
      wheel
      `c834a45cc3159ea082cf002ec1dd6e0b263c87047ea1b54fe6e29683633daf1a`),
      predecessor `0.208.54` /
      `9c8a4eed2858abd50bdcf27d7aab1b75f687f61c` (tree
      `c81fae29e659db22090559cde0352f37bc8413029e47109c4b552557146bf51e`,
      wheel
      `547a54c00c254c4df0a0d6f28f53705b1a09de62917527e1f314dd5c31aba7b8`).
      Journal `clear`; this hop's fallback is the same `0.208.54`
      sibling. DR published index_revision 268; retention remains
      `review_required`. Backend and `friday-bridge` active. Units
      hashes unchanged
      (`friday-backend.service`
      `17b997cad4d013f9b7950b56849d4e2dba495b8706d63c620503b718beeaf99d`,
      `friday-bridge.service`
      `846f8be6f28041301daba8518636c93c5478e767455531e90884494a61e1ed3e`).
      Durable S3 promoted-product row `evt_1827c6dff24b40b4` still
      present; do not delete it. Do not re-activate `e60860ea` or
      `9c8a4eed`. Next product sibling that changes friday sources is
      `assist_to_shadow`.
- [x] Luna-1 `s3-web-evidence-truncation` ACCEPT, cherry-picked as
      `2d6f8c16abcf1aab7a627aafdf494df208e05fce` onto `origin/main`.
      Honest empty upstream titles stay empty; the public URL remains.
      Do not invent titles.
- [x] Luna-2 `s3-synthesis-projection-budget` ACCEPT, cherry-picked as
      `ce9a10ed5a61ca5666372ef6a577dd851f067185` onto `origin/main`.
      Q38 dual-partial projection no longer burns a doubled verifier
      reserve.

Decision chain of the extra-hop restore (body-free; success-path
record the owner required in this audit commit, not a failure dump):

1. `0.208.51` 05:25Z consume left one durable
   `semantic_supervisor.promoted_product` row. Same-release env-only
   assist restore is forbidden (`candidate_rollback_identity_not_distinct`).
2. `0.208.53` `shadow_to_assist` issue against live `0.208.52` failed
   HTTP 400: old assist-issue path required
   `sample.promoted_product_events == 0`.
3. Product `0.208.54` (`9326cd64` / dense `9c8a4eed`): assist issue
   still fails closed if `precursor_assist_promotion_evidence_sha256`
   is not None; it no longer fails on historical promoted rows.
   Current-window promoted execution still fails closed via
   `shadow.actual_promoted_execution is False`.
4. `0.208.54` install-units required journal previous = live identity.
   Live was already `0.208.53` keep-shadow (`71479392`); 054
   CHANGELOG still lists predecessor `0.208.52` because it was sealed
   before that hop. Do not rewrite 054 docs after the fact.
5. Do not `shadow_to_assist` `0.208.53` after it became live (old
   issue path). 053 stayed the predecessor hop; 054 keep-shadow
   carried the product fix (DR 264).
6. `0.208.55` no-product extra-hop (`170475eb` / dense `e60860ea`).
   Assist issue posted against live 054; `shadow_to_assist` until
   health `0.208.55` / `assist` (DR 268).
7. Luna S3 residual packets above were already on `origin/main` in
   the `0.208.52` product sibling. CLOSE is Grok-only after this
   identity. No further Luna packet. Remaining owner-end: Pandora.

### Open and implementable

C1 correctness/containment and the existing N1–N5 product-completion work above
are implementable and active under the owner's extended reliability mission.
The prior audit/activation narrative is historical, not a blanket acceptance of
those user outcomes. Preserve the 05:25Z S3 witness and its honest partial evidence;
do not fabricate traffic, issue no-product hops for bookkeeping, or touch Pandora.

### Open and blocked

- [ ] Gemini parity needs a paired scored set. Do not claim it.
      Private N2 self-score is Friday's own gates, not Gemini.
      Owner-parked into Pandora 2026-09-05 (P0W cancelled; no Google billing;
      Gemini API Search grounding unavailable). Evidence:
      `handoffs/SolGoodman/P0W-CANCELLED-BY-USER-001.md`. Not `[x]`.

### Operating invariants (never "done", always in force)

- Keep deployed P0/P1 paths green; do not expand `EngineerWorkItem v1`.
- S3 consumed witness exists (durable 05:25Z graph and promoted-product
  row). Do not fabricate additional S3 traffic. Compact health after a
  cutover may still show `effective=off` (`material_loaded_not_accepted`);
  that is not a missing witness. Live assist is `0.208.55`.
- Primary-only path when the laptop/secondary is absent.
- One turn / one effect owner / one final publisher. No new orchestrator.
- Do not use Docker to certify primary Friday.
- Do not touch the Obsidian companion without a separate owner request.
- Do not merge old feature branches wholesale.
- No new product logic in `friday/agent_runtime/__init__.py` unless no
  narrow seam exists.
- During implementation: focused tests. Full exact-release gate only at a
  clean release boundary.
- Never overlap our full native/UI gate with SolGoodman's full gate.

## Canonical golden-journey/evidence registry

This is the single source of truth for product-level journey states. The
machine contract in `tests/test_golden_journey_registry.py` parses this table
directly; detailed trackers may link here but must not duplicate its states.

The readiness vocabulary is closed to `READY`, `DEGRADED`, `UNVERIFIED`,
`BLOCKED` and `OUT_OF_SCOPE`. Evidence is closed to `VERIFIED`, `AVAILABLE`,
`MISSING`, `STALE`, `FAILED` and `NOT_APPLICABLE`. `AVAILABLE` means that a
journey-specific contract, executable test or runbook exists, not that the
complete journey passed. Generic release, rollback and backup tests are not
journey evidence.

The validator now admits only a closed machine-produced receipt bound to the
exact commit, tree, wheel, schema and named executable tests, plus an
authenticated owner Telegram smoke where the journey crosses an external edge.
Public validation independently reruns the closed test inventory from the exact
source commit and rejects forged or mutable `PASSED`/`FAILED` outcomes even when
their surrounding manifest digests are recomputed.

A manifest and its sanitized receipt must use their single deterministic
privacy-safe paths derived from journey, class, result and the full release
identity. They bind the exact deployed source, tree, wheel and database schema,
closed executable-test node IDs, independently observed outcomes and SHA-256
digests of Git-blob source bytes at the manifest source commit, never the
mutable checkout. Closed allowlists forbid raw content, people, conversations,
prompts, responses, runtime paths, tool arguments, test bodies and logs. `READY`
requires every applicable journey class to be current `VERIFIED`; generic
release/rollback/backup proof stays at release level instead of being copied
into every row. Obsidian remains `UNVERIFIED` without current physical Android
evidence, unless current `FAILED` evidence makes it honestly `BLOCKED`. There
are no `READY` claims at this checkpoint.

| Journey ID | Journey | Readiness | deterministic contract | integration path | clean artifact path | synthetic live path | production read-only observation | physical device evidence | restart and recovery evidence | rollback evidence | backup and restore evidence | Limitation codes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `conversation_recall` | Conversation recall | `DEGRADED` | `AVAILABLE`<br>[friday/interaction_control_plane/work_item_contract.py](../friday/interaction_control_plane/work_item_contract.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py) | `AVAILABLE`<br>[friday/orchestration/message_window_outcome.py](../friday/orchestration/message_window_outcome.py)<br>[tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted](../tests/test_message_window_runtime_integration.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed](../tests/test_archive_search_runtime_publication.py) | `VERIFIED`<br>[evidence/golden_journeys/manifests/conversation_recall--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json](../evidence/golden_journeys/manifests/conversation_recall--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json) | `MISSING` | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update](../tests/test_message_window_work_item_runtime.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `semantic_recall_missing`<br>`cross_lane_coverage_missing` |
| `document_recall_answer` | Document recall and answer | `DEGRADED` | `AVAILABLE`<br>[friday/file_evidence_reader.py](../friday/file_evidence_reader.py)<br>[tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle](../tests/test_v12_file_evidence_reader.py) | `AVAILABLE`<br>[friday/orchestration/file_read.py](../friday/orchestration/file_read.py)<br>[tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections](../tests/test_v12_file_evidence_reader.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts](../tests/test_archive_search_runtime_publication.py) | `VERIFIED`<br>[evidence/golden_journeys/manifests/document_recall_answer--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json](../evidence/golden_journeys/manifests/document_recall_answer--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json) | `AVAILABLE`<br>[tools/document_contour_live_battery.py](../tools/document_contour_live_battery.py)<br>[tests/test_document_contour_live_battery.py::test_manifest_is_exactly_ten_unique_document_scenarios](../tests/test_document_contour_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts](../tests/test_archive_search_runtime_publication.py)<br>[tests/test_archive_search_runtime_publication.py::test_selected_archive_replay_failure_is_source_free_and_suspends](../tests/test_archive_search_runtime_publication.py) | `MISSING` | `MISSING` | `cross_lane_coverage_missing` |
| `obsidian_write_sync` | Obsidian write and synchronization | `UNVERIFIED` | `AVAILABLE`<br>[friday/organs/obsidian/contracts.py](../friday/organs/obsidian/contracts.py)<br>[friday/orchestration/effect_outcome.py](../friday/orchestration/effect_outcome.py)<br>[tests/test_effect_outcome.py::test_effect_outcome_is_immutable_canonical_closed_and_round_trips](../tests/test_effect_outcome.py)<br>[tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions](../tests/test_obsidian_structured_acceptance_core.py) | `AVAILABLE`<br>[friday/organs/obsidian/runtime.py](../friday/organs/obsidian/runtime.py)<br>[tests/test_agent_obsidian_acceptance_message_matrix.py::test_every_exact_tier_a_b_message_routes_through_full_chat_once](../tests/test_agent_obsidian_acceptance_message_matrix.py)<br>[tests/test_agent_obsidian_production_composition.py::test_note_create_append_and_daily_exact_messages_mutate_the_real_vault](../tests/test_agent_obsidian_production_composition.py) | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract](../tests/test_obsidian_syncthing_live.py) | `MISSING` | `MISSING` | `AVAILABLE`<br>[tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text](../tests/test_obsidian_runtime.py)<br>[tests/test_obsidian_operations.py::test_unproved_append_stays_uncertain_and_never_mutates_the_vault](../tests/test_obsidian_operations.py) | `MISSING` | `MISSING` | `physical_android_round_trip_missing`<br>`real_conflict_evidence_missing` |
| `durable_scheduled_work` | Durable scheduled work | `DEGRADED` | `AVAILABLE`<br>[friday/reminder_schedule.py](../friday/reminder_schedule.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `AVAILABLE`<br>[friday/storage/_missions.py](../friday/storage/_missions.py)<br>[tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model](../tests/test_a_reminder_is_set_before_the_model_speaks.py) | `VERIFIED`<br>[evidence/golden_journeys/manifests/durable_scheduled_work--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json](../evidence/golden_journeys/manifests/durable_scheduled_work--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json) | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary](../tests/test_synthetic_live_battery.py) | `STALE`<br>[evidence/golden_journeys/manifests/durable_scheduled_work--production_read_only--verified--b02177bb427c5ff5aa537ffb7460a69aec897a8ac9eb573f5a272eb0105c68e8.json](../evidence/golden_journeys/manifests/durable_scheduled_work--production_read_only--verified--b02177bb427c5ff5aa537ffb7460a69aec897a8ac9eb573f5a272eb0105c68e8.json) | `NOT_APPLICABLE` | `VERIFIED`<br>[evidence/golden_journeys/manifests/durable_scheduled_work--restart_recovery--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json](../evidence/golden_journeys/manifests/durable_scheduled_work--restart_recovery--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json) | `MISSING` | `MISSING` | `journey_specific_rollback_backup_evidence_missing` |
| `honest_degradation` | Honest degradation | `DEGRADED` | `AVAILABLE`<br>[friday/orchestration/capability_outcome.py](../friday/orchestration/capability_outcome.py)<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result](../tests/test_search_provider_refusal_is_not_emptiness.py) | `AVAILABLE`<br>[tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses](../tests/test_search_provider_refusal_is_not_emptiness.py)<br>[tests/test_message_window_runtime_integration.py::test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried](../tests/test_message_window_runtime_integration.py) | `VERIFIED`<br>[evidence/golden_journeys/manifests/honest_degradation--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json](../evidence/golden_journeys/manifests/honest_degradation--clean_artifact--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json) | `AVAILABLE`<br>[tools/synthetic_live_battery.py](../tools/synthetic_live_battery.py)<br>[tests/test_synthetic_live_battery.py::test_full_package_a_oracle_accepts_natural_honest_refusals](../tests/test_synthetic_live_battery.py) | `MISSING` | `NOT_APPLICABLE` | `VERIFIED`<br>[evidence/golden_journeys/manifests/honest_degradation--restart_recovery--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json](../evidence/golden_journeys/manifests/honest_degradation--restart_recovery--verified--27d26dbd1ea9250dc1a947082449b947bc38578f81cabfd93938b3a78594aabd.json) | `MISSING` | `MISSING` | `product_multi_lane_coverage_missing` |
| `current_file_web_comparison` | Current file and web comparison | `UNVERIFIED` | `AVAILABLE`<br>[tests/test_compare_current_file_web_work_graph_schema45.py::test_schema45_exact_binding_is_durable_immutable_and_revision_cas](../tests/test_compare_current_file_web_work_graph_schema45.py) | `AVAILABLE`<br>[tests/test_supervisor_assist_controller.py::test_review_and_web_recovery_are_strictly_bounded](../tests/test_supervisor_assist_controller.py) | `MISSING` | `MISSING` | `MISSING` | `NOT_APPLICABLE` | `AVAILABLE`<br>[tests/test_supervisor_assist_graph_adapter.py::test_terminal_cancel_and_startup_reconcile_publish_closed_receipts](../tests/test_supervisor_assist_graph_adapter.py) | `MISSING` | `MISSING` | `assist_promotion_evidence_missing`<br>`clean_release_artifact_missing`<br>`activation_rollback_evidence_missing` |

## Update rule

After every production release update the source/live/fallback identities,
health, completed package, active package, evidence rows and next order here.
Never mark device-dependent or external-service observations complete from
local tests. No other tracked file may become a mutable backlog or status log.
