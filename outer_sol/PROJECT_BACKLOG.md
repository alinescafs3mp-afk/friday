# Friday: canonical project backlog

Updated: 2026-09-04 (live `0.208.18`; N1 one-final-carrier live; remaining N2 observation-bound; N3 result-archive-pack and N4 mixed-journey view on `origin/main` and unwired)

This is the project's only backlog and mutable status register. It owns the
current production identity, execution order, acceptance gaps and owner actions.
Architecture, operations and acceptance documents are immutable design inputs;
they must link here for live state and may not carry a competing task list.

Old agent task files, handoffs, dated reports and superseded status registers are
kept in Git history, not in the working tree. A task discovered anywhere else is
either merged here or discarded before that source is removed.

The 2026-09-03 post-backlog audit snapshot (`main` `43a16c8b`, source
`0.208.4`, recorded production `0.208.1`) is expired. Independent re-audit of
the live host and current `origin/main` is the source of the identity below.
Do not copy that snapshot's release claims forward.

## Current production identity

- Branch: `main`
- Deployed implementation head: `8b6a8c13ce54b8b07192cb6f5b820953da4efcb5`.
- Live: Friday `0.208.1` / `8b6a8c13ce54b8b07192cb6f5b820953da4efcb5`;
  tree `5587e4c3dd45e2b85191b8d39dd0268dc7fbf046526761af6d485297d2be8f82`;
  wheel `1cadf5769b87f9cdc152729183ddf3a1d6ef4a8ff3192d48d1a5b538e856872f`.
- Immediate runtime predecessor: Friday `0.208.0` /
  `75b165a23809dfcc7445311e2dc896c98ce3df00`; tree
  `9d1c49da576e58e73ec1570d3e4c7e1ea7ebca2d44cc1e7482ef418c9ec89315`.
  Schema-capable fallback: Friday `0.207.90` /
  `7abb3c5e3fb29bdc7c53bf923f8b218fa26f07e9`; tree
  `c1c29331db489ad1c56080d70a8c37d4051b4752f1309dba9c0a012099ebcae5`.
- Database schema: 50 in deployed production and the fallback.

Golden-journey receipts stay bound to that `0.208.1` root until rebound. The
running sealed sibling is Friday `0.208.18` at
`19d1b89cbe5ebb9763b95c97dae6d8ebb03ad669` (tree-file
`013f76d0e502a1b03e673ef87c32118a26c9acec1df36b7b4f81348b4acda483`, wheel
`b520a5d117106b0acb654fa8cafa18ee2775d09b81db2075a57962bedcffbac1`, journal
`clear`, predecessor `0.208.17` /
`426729dc6dc0dc6839eeb537d09432dcbdb551f9`). Trusted-CA health reports
`version=0.208.18`. S3: `requested_mode=assist`, `effective_mode=off`,
`promotion_admitted=false`,
`activation.reason=material_loaded_not_accepted`. Advice stays off until
a genuine eligible witness; do not fabricate traffic. Next product
sibling that changes friday sources is `assist_to_shadow`. `origin/main`
is the live sibling. N1 Telegram surfaces (ordinary `/chat` status,
Engineer status renderer, file-album DOCUMENT status, Engineer
FILE/ARCHIVE carrier, restart/edit-reject/send-fence/cancel counts,
observed web/archive chat status) and one-final-carrier packing are
live. N2 live `_web_research` refuses private observed URLs,
empty-after-outbound, and invalid provider facts; `_web_search` and
`_web_fetch` refuse `BLOCKED_PRIVATE` the same way. `search`/`research`
observe chain names as `WebProviderId` and the kernel consumes
`select_web_provider` when `selected_provider_id` is present. File+web
comparison and `POST /api/ingest/url` refuse observed private URLs via
the same consumption gate. Other N2 contracts stay observation-bound:
live requesting workflows do not observe claims, dates, passages or
missions, and `/coding` does not exist. Do not fabricate those
witnesses. N3
inspect, extract-plan, prompt-to-small-project, isolated-worker,
result-archive-plan and upload-modification-admission families are live
as source and unwired. N4 shared
operation/situation view contracts are on `origin/main` and unwired.

- Production: immutable activation `phase=clear`; backend and Telegram bridge
  active; writer target `candidate`. Retention admission remains honestly
  `review_required` and grants no apply/delete authority. V12 `canary_ready`
  with verified, installation and effective context all exactly `40960`.
- Secondary: accepted/live GPT-OSS profile `gptoss20b-2335df…`; `state=healthy`,
  `mode=assist`, `available=false`. Supervisor advice stays off until a genuine
  eligible current-file-plus-public-web consumed witness; no traffic is
  fabricated. Primary-only behavior is preserved when the laptop is absent.
- The reader-first body-free document-passage contour is fully converged: 1,720
  current parents and 16,359 child passages, with no pending v3 backfill. The two
  formerly invalid sparse-text v2 sources were repaired by the released v3
  topology; no document body is duplicated in the sidecar.
- The body-free conversation-passage writer and lexical lane are live. The
  first two observed bounded worker ticks advanced 66 authenticated anchors;
  restart-safe production backfill remains active and keeps every unfinished
  projection explicitly `backfill_pending`.

## Active package

The old S0–S6 implementation queue has converged on production `0.208.11`.
S4-R8 formal cutover is code-owned `accepted`: dialogue offers `archive_search`;
`memory_search` / `source_search` / `message_search` remain internal. Exact
window, temporal and graph lanes dispatch through that facade; a generic
continuation cannot mint a fresh exact selector. S3 assist-controller is
deployed; advice remains off until the observation-bound witness. S5 40k lease
and S6 recovery/browse paths stay live.

The live product queue is N1–N5. The shared operation-progress contract is
live. Production `0.208.18` renders ordinary Telegram `/chat` status,
Engineer status, file-album DOCUMENT status, Engineer FILE/ARCHIVE
carrier, observed web/archive chat status, and one-final-carrier packing
through that projection. N2 currentness, evidence bundle,
multi-query mission planner, provider fallback, source diversity,
consumption, readiness and citation coverage, claim support, answer
admission, contradiction coverage, exact mission coverage, evidence
grounding, source date coverage, claim currentness and
passage-reference coverage are on `origin/main` and live as source.
Live `_web_research` refuses private URLs, empty-after-outbound and
invalid provider facts; `_web_search` and `_web_fetch` refuse
`BLOCKED_PRIVATE`; observed chain names are consumed when
`selected_provider_id` is present. File+web comparison and
`POST /api/ingest/url` consume `BLOCKED_PRIVATE`. Other N2
contracts stay observation-bound; do not fabricate witnesses. N3
project identity, archive extract admission,
bare-source inspection, extract-plan family, prompt-to-small-project,
isolated-worker contracts, one-final source-archive plan,
result-archive-pack family and upload-modification admission are on
`origin/main` and are not wired. Live `/coding` remains. N4 shared
operation/situation view and mixed-journey view contracts are on
`origin/main` and are not wired to stores or Telegram. N5
extracted the kernel web-consumption seam; maintainability follows. Physical Android, P0H deletion, off-machine
mirror and provider-credential rotation remain owner-parked.

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
cutover deployed in `0.208.11`. Advice remains observation-bound: requested
assist, effective off, `promotion_admitted=false`.

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
witness is still absent, so assist correctly remains off instead of trusting a
file, prompt claim or fabricated observation.

Remaining promotion work is observation-bound, not implementation-bound.
The `0.208.11` controller consume is done; `promotion_admitted` still requires
a genuine eligible current-file-plus-public-web consumed witness. Do not
fabricate traffic. Assist still does not change the sealed query, does not
own synthesis, and does not expand into coding or research until this
journey demonstrates non-regressive value.

The primary keeps files local and sends only an independent sealed public-web
topic. Current files (one or several), a restored prior file, and an unused
current upload no longer veto an independent public-web clause. Same-sentence
summarize without a sealable public topic stays local and skips web instead of
refusing the whole turn. File-as-query and leftover file nouns still fail
closed for the outbound query. Advice stays off until a genuine consumed
representative-window witness exists.

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
sealed sibling are `0.208.11` / `6b61987a`. S3 assist-controller consume is
done. Remaining N0 row is the observation-bound promotion witness, which
stays in Owner/external and must not block N1.

- [x] Reconcile source candidate, production, fallback, journals and evidence.
- [x] Deploy or explicitly supersede the `0.208.2`–`0.208.10` candidate chain.
- [x] Record exact live identity in this register (`0.208.11`, schema 50,
      predecessor `0.208.10`, activation `clear`).
- [ ] S3 advice on a genuine eligible turn — observation-bound, not an
      implementation package. Do not fabricate traffic.

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

Status: modules started on `origin/main` and live as source in `0.208.18`.
Today is still a strong single-query 27s/3-source (max 8) live pipeline.
Code-owned currentness policy, frozen `WebEvidenceBundleV1`, multi-query
mission planner, provider fallback policy, source diversity, research
consumption, readiness composition, citation coverage, claim support,
answer admission, contradiction coverage, exact mission coverage,
evidence grounding, source date coverage, claim currentness and
passage-reference coverage exist and are not wired into Coding journeys.
Live `_web_research` refuses private observed URLs, empty-after-outbound
(`no_admitted_sources`) and invalid provider facts; empty success is not
completeness. `_web_search` and `_web_fetch` refuse `BLOCKED_PRIVATE`.
File+web comparison and `POST /api/ingest/url` consume `BLOCKED_PRIVATE`
the same way. Remaining N2 contracts stay observation-bound: live
requesting workflows do not observe claims, dates, passages or missions.
Table CSV/spreadsheet URLs already go through file+web consumption.
Coding `/coding` does not exist. Do not wire mission/answer-gate into
live single-query `_web_research`. Do not fabricate those facts.

- [x] Automatic currentness / knowledge-gap policy module on `origin/main`
      (`friday/orchestration/web_currentness_policy.py`). Not wired into
      live `web_surfer` or Telegram. Private filenames, paths and deictics
      stay local.
- [x] Multi-query research mission planner on `origin/main`
      (`friday/orchestration/web_research_mission.py`). Emits 2–8
      complementary public queries; `SEARCH_BLOCKED_PRIVATE` never becomes a
      query plan. Not wired.
- [x] Provider selection and honest degraded-fallback policy on
      `origin/main` (`friday/orchestration/web_provider_policy.py`). Not
      wired into live `web_surfer`. PRIMARY_OK / FALLBACK_USED /
      DEGRADED_PARTIAL / UNAVAILABLE; empty success is refused.
- [x] `WebEvidenceBundleV1` contract on `origin/main`
      (`friday/orchestration/web_evidence_bundle.py`). Not consumed by the
      requesting workflow yet.
- [x] Public-web source diversity on `origin/main`
      (`friday/orchestration/web_source_diversity.py`). Lexical hostname
      only; EMPTY / SINGLE_HOST / CONCENTRATED / DIVERSE. Not wired.
- [x] Research consumption gate on `origin/main`
      (`friday/orchestration/web_research_consumption.py`). CONSUMABLE /
      CONSUMABLE_DEGRADED / BLOCKED_PRIVATE / UNAVAILABLE. Live kernel
      `_web_research`/`_web_search`/`_web_fetch`, file+web comparison and
      `POST /api/ingest/url` refuse `BLOCKED_PRIVATE`; kernel research also
      refuses empty-after-outbound. Other states are not yet consumed
      by the requesting workflow.
- [x] Research readiness composition on `origin/main`
      (`friday/orchestration/web_research_readiness.py`). READY /
      READY_DEGRADED / NOT_READY from mission, diversity and consumption.
      Not wired.
- [x] Citation host coverage on `origin/main`
      (`friday/orchestration/web_citation_coverage.py`). COMPLETE / PARTIAL /
      EMPTY / BLOCKED_PRIVATE; lexical hostname only. Not wired.
- [x] Claim-support contract on `origin/main`
      (`friday/orchestration/web_claim_support.py`). COMPLETE / PARTIAL /
      EMPTY / UNSUPPORTED / BLOCKED. Contradicting-only and unknown source
      ids are not support. Not wired.
- [x] Answer-admission gate on `origin/main`
      (`friday/orchestration/web_research_answer_gate.py`). ADMITTED /
      ADMITTED_DEGRADED / HOLD / BLOCKED from readiness and citation
      coverage. Not wired.
- [x] Contradiction coverage on `origin/main`
      (`friday/orchestration/web_contradiction_coverage.py`). EMPTY / NONE /
      PRESENT / UNIVERSAL / BLOCKED. Supporting-only is not contradiction.
      Not wired.
- [x] Exact mission coverage on `origin/main`
      (`friday/orchestration/web_mission_coverage.py`). COMPLETE / PARTIAL /
      EMPTY / BLOCKED. Extra executed queries do not complete coverage.
      Not wired.
- [x] Evidence grounding on `origin/main`
      (`friday/orchestration/web_evidence_grounding.py`). EMPTY / GROUNDED /
      PARTIAL / UNGROUNDED / BLOCKED. A claim is grounded iff an admitted
      supporting or contradicting source id is present. Not wired.
- [x] Source date coverage on `origin/main`
      (`friday/orchestration/web_source_date_coverage.py`). EMPTY / DATED /
      PARTIAL / UNDATED / BLOCKED. `retrieved_at` alone is not dating.
      Not wired.
- [x] Claim currentness admission on `origin/main`
      (`friday/orchestration/web_claim_currentness.py`). EMPTY / ADMITTED /
      HOLD / BLOCKED. `SEARCH_NOT_REQUIRED` does not admit current-sensitive
      claims. Not wired.
- [x] Passage-reference coverage on `origin/main`
      (`friday/orchestration/web_passage_reference_coverage.py`). EMPTY /
      REFERENCED / PARTIAL / BARE / BLOCKED. Title, digest and `retrieved_at`
      are not references. Not wired.
- [ ] Remaining N2 contracts stay observation-bound until a live workflow
      observes claims, dates, passages or missions. Table file+web and
      kernel/ingest paths already consume `BLOCKED_PRIVATE`. Do not wire
      mission/answer-gate into live single-query `_web_research`. Do not
      fabricate witnesses.
- [ ] Private representative benchmark and body-free public summary. Do not
      claim Gemini parity without a paired scored set.

### N3 — Coding Mode

Status: modules started on `origin/main`. `/coding` does not exist.
Engineer bubblewrap is not a Coding Worker. Prompt-to-small-project,
isolated-worker and one-final source-archive plan contracts are on
`origin/main` and unwired. Until a live isolated worker exists, static
inspect/edit only; do not claim safe build/test of untrusted uploads.

- [x] Bare-source inspection contracts on `origin/main`
      (`coding_source_member.py`, `coding_source_tree.py`,
      `coding_source_inspect.py`, `coding_inspect_hazards.py`,
      `coding_toolchain_hint.py`, `coding_inspect_report.py`).
      EMPTY / MAPPED|INSPECTED|HINTED / BLOCKED. No execute, no
      rebuild, no file I/O, filename-suffix hints only. Not wired.
- [x] Prompt-to-small-project and uploaded-project modification.
      Prompt normalization, implementation plan, scaffold, create
      admission and upload-modification admission are on `origin/main`
      (not wired; no execute, no `/coding`).
- [x] Persistent project identity contract on `origin/main`
      (`friday/orchestration/coding_project_identity.py`). EMPTY /
      IDENTIFIED / BLOCKED. Exact revision only; `latest`/`HEAD`/
      `newest`/`current` fail closed. Not wired to git or a worker.
- [ ] Isolated coding worker: no host secrets, no Docker socket, no
      production database, bounded network. Frozen identity, isolation,
      network, workspace, limits and admission contracts are on
      `origin/main` (not wired; no process, Docker, or `/coding`).
- [x] Safe archive extract admission on `origin/main`
      (`friday/orchestration/coding_archive_extract_admission.py`).
      EMPTY / ADMITTED / BLOCKED from member metadata. Traversal,
      absolute paths, symlink/hardlink, device, size, bomb ratio,
      file count, nesting and case-fold collisions fail closed. Not
      wired; no archive is opened.
- [x] Archive extract-plan family on `origin/main`
      (`coding_archive_member_catalog.py`,
      `coding_archive_extract_plan.py`,
      `coding_archive_digest_facts.py`,
      `coding_project_isolation_admission.py`,
      `coding_archive_overwrite_plan.py`). Catalog, relative
      destinations, supplied SHA-256 facts, project-root
      isolation and overwrite/collision. No archive is opened.
      Not wired.
- [x] One final source archive; restart, rollback and adversarial proof.
      TEXT/FILE/ARCHIVE plan, manifest, pack admission, publication,
      restart, rollback and uncertainty contracts are on `origin/main`
      (`coding_result_archive_plan.py`,
      `coding_result_archive_manifest.py`,
      `coding_result_archive_pack_admission.py`,
      `coding_result_publication_admission.py`,
      `coding_result_restart_admission.py`,
      `coding_result_rollback_admission.py`,
      `coding_result_uncertainty.py`); no archive is packed or opened.
      Not wired.

Do not prebuild a compiler catalogue. Do not weaken Engineer Mode or
primary release certification to create the worker.

### N4 — Whole-Organism Coherence

Status: contracts started on `origin/main`. Durable organs exist; the
shared view is not yet derived from live stores. Primary and secondary
must share one operation identity. New modes compose existing primitives.

- [x] Read-only `SharedOperationViewV1` / `AgentSituationProjectionV1`
      on `origin/main` from already-supplied facts. No new execution
      owner. Not wired to stores or Telegram.
- [x] Mixed-journey view contracts on `origin/main`
      (`mixed_journey_identity.py`, `mixed_journey_organs.py`,
      `mixed_journey_coverage.py`, `mixed_journey_revoke.py`,
      `mixed_journey_restart.py`, `mixed_journey_view.py`). EMPTY /
      PROJECTED / BLOCKED from already-supplied facts. No store or
      Telegram mix. Not wired.
- [ ] Mixed journeys live: file+archive+conversation+web+table;
      Engineer+advisories; Coding+current docs; restart during
      status+execution; revoke-before-publish. Contracts exist; live
      mix is not derived from stores.
- [ ] One turn, one operation, one status, one result, one effect owner,
      one publisher. Honest `UNKNOWN`. Primary-only when secondary is absent.

### N5 — Maintainability Ratchet

Status: standing rule, not a rewrite. `friday/agent_runtime/__init__.py` is
~76k lines / 3.81 MiB. No new product logic in that module unless no narrow
seam exists. Extract only a touched seam with exact parity tests. Do not
begin a clean-architecture rewrite. Kernel web-consumption helpers now live
in `friday/execution_kernel/web_consumption.py`; `_web_search`/`_web_fetch`/
`_web_research` still own quota and adapter I/O.

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

## Owner/external actions

- S3 Supervisor promotion remains observation-bound: one genuine eligible
  current-file-plus-public-web consumed witness. Do not fabricate traffic.

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
      `effective=off` until the consumed witness)
- [x] N0 identity reconciliation: `main` = production = `6b61987a` /
      `0.208.11`; candidate chain `0.208.2`–`0.208.10` superseded

### Closed on production `0.208.18`

- [x] N1 two-message Telegram UX: `/chat`, Engineer, file-album,
      FILE/ARCHIVE carrier, observed web/archive status and one-final-carrier
      packing live on `0.208.18`

### Open and implementable

- [ ] N2 currentness, evidence bundle, mission, provider fallback, source
      diversity, consumption, readiness, citation coverage, claim support,
      answer admission, contradiction coverage, exact mission coverage,
      evidence grounding, source date coverage, claim currentness and
      passage-reference coverage are on `main` and live as source; live
      `_web_research` refuses private URLs, empty-after-outbound and
      invalid provider facts; remaining contracts stay observation-bound
      without fabricated witnesses
- [ ] N3 Coding Mode MVP behind an isolated worker; project identity,
      extract admission, bare-source inspection, extract-plan family,
      prompt-to-small-project, isolated-worker, source-archive plan,
      result-archive-pack family and upload-modification admission are
      on `main` and unwired. Live `/coding` remains.
- [ ] N4 shared operation/situation and mixed-journey view contracts
      are on `main` and unwired; live mixed-organ journeys are not
      derived from stores
- [ ] N5 extract only touched seams from giant runtime modules;
      kernel web-consumption helpers live on `0.208.18` in
      `friday/execution_kernel/web_consumption.py`

### Open and blocked

- [ ] S3 Supervisor advice on live turns — observation-bound. Need a genuine
      eligible current-file-plus-public-web consumed witness. Do not
      fabricate traffic.

### Operating invariants (never "done", always in force)

- Keep deployed P0/P1 paths green; do not expand `EngineerWorkItem v1`.
- Keep S3 advice off until the exact production witness exists. The
  assist-controller may already be requested; that is not promotion.
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
