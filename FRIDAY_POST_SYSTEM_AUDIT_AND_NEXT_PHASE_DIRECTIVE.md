# FRIDAY POST-BACKLOG SYSTEM AUDIT
## Next-Phase Product and Architecture Directive for Grok

**Audience:** Grok, permanent Lead Architect and Release Captain
**Snapshot date:** 2026-09-03
**Repository snapshot reviewed:** `main` at `43a16c8b8d790ee4b8c6d6d73ed2c3cf5593ab81`
**Source version at that snapshot:** `0.208.4`
**Deployed production recorded by the canonical backlog:** `0.208.1` at `8b6a8c13ce54b8b07192cb6f5b820953da4efcb5`
**Database schema:** `50`
**Canonical mutable register:** `outer_sol/PROJECT_BACKLOG.md`

This document is an architecture audit and implementation directive. It is not
an exact-release certificate and does not replace the canonical backlog,
production receipts, the full native/UI gate, rollback proof, or live owner
acceptance.

The snapshot section expires as soon as `main` or production changes. Re-read
the actual repository, canonical backlog, release evidence, host state, and
current `Ctrl+T` checklist before acting.

---

# 0. Permanent operating instructions

These instructions supplement the existing Grok Lead Architect Succession
Directive. They do not weaken any rule inherited from Sol and SolGoodman.

## 0.1 Communication with the owner

Communicate with the owner exclusively in Russian.

Do not narrate routine work, paste running logs, or report every small commit.
Send a message only when one of these events occurs:

1. a genuine blocker requiring owner input;
2. a material architecture or trust-boundary decision;
3. an important checkpoint that changes what is safe to do next;
4. a production release or rollback has completed;
5. the complete assigned phase is finished.

Reports must be concise, factual, and bound to exact evidence. Distinguish:

- implemented;
- integrated on `main`;
- independently accepted;
- release-certified;
- deployed;
- observed in production;
- owner/device-bound;
- blocked external.

Never use "the backlog is complete" as a substitute for those states.

## 0.2 Canonical planning

`outer_sol/PROJECT_BACKLOG.md` remains the only tracked mutable backlog and
status register.

Maintain the operator-visible `Ctrl+T` checklist continuously. It must show the
current executable projection of the canonical backlog:

- one active item;
- the next bounded items;
- delegated or waiting items;
- real blockers;
- the exact release boundary.

The `Ctrl+T` checklist is not a second source of truth. Reconstruct it from Git,
the canonical backlog, task state, release state, and actual evidence after
restart or session rotation.

Do not create another roadmap, live handoff register, mutable audit file, or
parallel implementation-status document.

## 0.3 Inherited engineering doctrine

Preserve the strongest rules established by Sol and SolGoodman:

- one accepted turn has one identity;
- one turn has one authorized source set;
- one turn has one inherited deadline;
- one turn has one effect owner;
- one turn has one final publisher;
- one task packet has one owner;
- one shared write surface has one writer;
- one production line has one Release Captain;
- the primary remains the sole tool, effect, and final-publication owner;
- secondary models remain bounded advisors;
- fallback preserves the primary-only path;
- authorization, provenance, final reauthorization, cancellation, honest
  `UNKNOWN`, replay fences, and exactly-once publication remain mandatory;
- no model summary, green test, or implementation presence self-promotes a
  review state;
- no release is accepted by weakening, skipping, shrinking, or rewriting its
  gate to match the implementation;
- no live production file, installed wheel, environment, database, sealed
  evidence file, or running container is patched in place;
- product changes flow through Git, focused validation, independent review where
  required, exact-release certification, immutable activation, and rollback;
- prefer a small reversible repair to a new subsystem;
- do not add a store, WorkGraph, observer, PKI, scheduler, or orchestration layer
  unless a reproduced product journey proves it necessary;
- never expose credentials, private paths, private document text, or internal
  authority identifiers in logs, reports, prompts, or public evidence.

---

# 1. Executive verdict

## 1.1 Is the old canonical implementable backlog complete?

**Substantially yes.**

The old S0-S6 implementation queue has converged. Production `0.208.1` contains
the accepted sole-`archive_search` cutover, exact conversation windows,
bitemporal and graph retrieval, the one-turn authority system, restart-safe
Engineer continuation, measured cognition leases, the bounded second-brain
foundation, recovery paths, and refreshed exact-release evidence.

The canonical register truthfully says that no old item is currently both open
and implementable. Remaining old rows are observation-bound, owner/device-bound,
or destructive-maintenance-bound.

## 1.2 Is all current code deployed and product-accepted?

**No.**

At the reviewed snapshot:

- production was `0.208.1`;
- repository source declared `0.208.4`;
- `main` contained post-production work for local files beside public web,
  Semantic Supervisor timeout repair, and a distinct S3 assist candidate;
- the current-file-plus-web journey was still not a verified production journey;
- S3 assist was still awaiting its exact activation consume and production
  evidence;
- no public GitHub status contexts or workflow runs were attached to the reviewed
  `main` head.

This is not necessarily a defect. It is a release-state distinction that must be
resolved before a new large implementation wave begins.

## 1.3 Is Friday now a strong foundation for a reliable autonomous agent system?

**Yes.**

Friday's strongest quality is no longer the number of tools. Its strongest
quality is that identity, authority, source provenance, durable continuation,
effect ownership, final publication, and recovery are increasingly aligned.

The system is already a credible agent platform. It can accept a complex
owner request, bind the correct sources, run tools, survive restarts, continue
some durable work, recover exact results, and publish under one authority.

## 1.4 Is Friday already a fully coherent "living organism"?

**Not yet.**

The code-owned nervous system is strong, but the product still has several
partially independent stage vocabularies, work-item types, notification paths,
and mission implementations. The primary and secondary brains are safer and
better informed than before, but they still do not receive one compact,
system-wide situation projection describing the current operation, its plan,
active step, authorized sources, pending durable work, available capabilities,
and completion evidence.

The most visible symptom is notification behavior:

- ordinary chat already has one durable editable status message;
- Engineer has a durable status primitive, but also separate progress,
  terminal-text, terminal-artifact, archive, and delivery paths;
- the user can receive a pile of messages and archives even though the underlying
  operation is one coherent task.

The next phase should therefore improve **product coherence**, not construct
another generic execution engine.

---

# 2. Audit method and evidence limits

This review inspected:

- `main` history since `dde10eb4d557b195887def60d6f205916481e056`;
- the canonical backlog and journey registry;
- the current source version and changelog;
- the R8E/R8F cutover contracts and tests;
- document dense-recall evidence;
- `archive_search` and exact retrieval surfaces;
- Semantic Supervisor and second-brain source;
- current-file-plus-public-web query isolation;
- Engineer and Telegram notification delivery paths;
- durable work-item and interaction-control-plane structure;
- web provider and research implementation.

This review did **not** independently deploy the release, access the production
host, consume the pending S3 activation, execute the full native/UI gate, or run
a live Telegram journey. The reviewed GitHub head had no attached public CI
status or workflow run. Grok must independently reproduce the relevant tests and
live observations before changing production state.

Repository-declared evidence is meaningful, but it is not a substitute for a
fresh exact-release run and real owner journey.

---

# 3. Development since the previous audit

The previous review ended while R8E was integrated but not accepted and
production remained `0.208.0`.

Since then, the project made several material advances.

## 3.1 R8E was independently accepted

The exact bitemporal and graph memory lane gained a recorded independent
acceptance for reviewed SHA `f44c4e7c`. This opened formal cutover readiness
without treating green tests as self-approval.

## 3.2 The sole retrieval facade reached production

Production `0.208.1` now presents `archive_search` as the sole ordinary
model-facing archive and memory search tool.

The old tools remain available only as declared internal or mission adapters:

- `memory_search`;
- `source_search`;
- `message_search`.

Exact conversation windows, `as_of`, `known_at`, and bounded graph retrieval
are derived and dispatched through the shared archive owner. One final publisher
consumes the exact receipts.

## 3.3 Generic continuation was fenced

A generic continuation can no longer mint a fresh exact message, temporal, or
graph selector. It must redeem its existing opaque continuation first.

This is an important repair. It prevents a resumed model call from expanding
authority by adding new exact flags.

## 3.4 Current local files and public web can coexist

Post-`0.208.1` work added a strict public-topic extraction path:

- local file bytes and filenames stay local;
- only a sealed independent public topic may be sent to search;
- file deictics, file-as-query speech, unsafe remnants, and vague topics fail
  closed;
- an unusable web clause does not have to invalidate the local-file task.

This is a real step toward mixed-source work.

## 3.5 The Semantic Supervisor timing defect was repaired

The Supervisor's 12-second budget previously began before the primary finished.
A long primary response could consume the entire secondary budget before any
secondary HTTP request occurred.

The candidate repair starts the Supervisor budget at actual dispatch after the
primary await, while still respecting the inherited turn deadline.

## 3.6 A distinct S3 assist candidate exists

Source version `0.208.4` is allocated as a distinct evidence-gated
shadow-to-assist candidate. It remains off until the intended one-shot
operator consume and exact activation evidence.

This is the correct direction, provided the candidate chain is reconciled and
certified rather than merely version-bumped.

---

# 4. Detailed system audit

# 4.1 Release, backlog, and evidence truth

## Strong

- The canonical backlog distinguishes production, `main`, fallback, owner-bound
  work, observation-bound work, and evidence state.
- R8E did not self-promote from `integrated` to `accepted`.
- Production `0.208.1` was recorded with exact commit, tree, wheel, schema, gate,
  activation, and fallback identities.
- Golden-journey receipts were rebound to the deployed release instead of being
  left falsely current.
- The project keeps a schema-capable fallback.
- The old implementable queue is genuinely small rather than hidden behind a
  new architectural package.

## Weak

- `main` is several candidate versions ahead of production.
- The source version is `0.208.4`, while the canonical production register still
  says `0.208.1`.
- Rebinding dense evidence after every candidate version bump creates churn and
  makes it easy to confuse "same ranking metrics" with "certified complete
  release".
- The journey registry still contains no `READY` claim.
- Several important journeys remain `DEGRADED` or `UNVERIFIED`.
- The current GitHub head has no attached public CI status or workflow run, so
  outside observers cannot independently see a green candidate.

## Required action

Before opening the new product backlog, Grok must reconcile:

1. actual `main`;
2. actual source version;
3. production version;
4. pending release journals;
5. S3 activation state;
6. exact-release evidence;
7. rollback/fallback identity;
8. candidate versions `0.208.2`, `0.208.3`, and `0.208.4`.

Choose one truthful disposition:

- certify and deploy the latest coherent candidate;
- split and release only the accepted subset;
- supersede abandoned candidates explicitly;
- or return source metadata to an unreleased development state using the
  repository's established release process.

Do not begin a large notification or coding-mode release from an ambiguous
candidate chain.

---

# 4.2 Document ingestion, indexing, retrieval, and answering

## Strong

The document contour is now one of Friday's strongest technical subsystems.

### Ingestion and projection

The canonical state reports:

- 1,720 current document parents;
- 16,359 child passages;
- no pending document v3 backfill;
- no document-body duplication in the sidecar;
- repaired sparse-text sources;
- body-free public evidence.

### Retrieval architecture

The system now has:

- one ordinary dialogue facade, `archive_search`;
- lexical document and message lanes;
- fail-soft dense passage retrieval;
- exact focused-source retrieval;
- current-revision reauthorization;
- exact conversation windows;
- valid-time and transaction-time memory retrieval;
- bounded graph retrieval;
- restart and continuation protection;
- one final publisher.

This is a coherent retrieval surface rather than a model-facing collection of
partially overlapping search tools.

### Measured ranking evidence

The current dense-recall evidence is promising:

- synthetic corpus: 140 documents and 24 qrels;
- lexical baseline recall@10: 12/24;
- lexical baseline recall@20: 13/24;
- dense candidate recall@10: 24/24;
- dense candidate recall@20: 24/24;
- no unauthorized foreign source returned;
- a separate Qwen3 embedding backend observation measured 20/24 dense recall@10
  versus 14/24 lexical recall@10;
- production vector coverage records 1,088 knowledge objects, 1,570 object
  vectors, and 14,094 passage vectors.

The improvement is real on the declared corpus.

## Weak

The evidence file explicitly states that it does not measure:

- the production owner corpus;
- model-visible retrieval output;
- end-to-end execution-kernel behavior;
- final answer quality;
- citation correctness;
- production embedding quality over the owner's actual material;
- a release threshold for real-world readiness.

The canonical journey registry still marks:

- conversation recall as `DEGRADED`;
- document recall and answer as `DEGRADED`;
- cross-lane coverage as missing;
- semantic recall coverage as missing for conversations;
- production read-only observation as missing for document recall.

A 24-query synthetic benchmark is a good regression instrument. It is not proof
that Friday will reliably find:

- a document by approximate date;
- a table row buried in an Office file;
- a fact expressed in another language or script;
- an alias used only once;
- a scanned or poorly extracted PDF;
- the correct historical revision;
- a document referred to indirectly in a conversation;
- a file plus related messages plus current web evidence;
- contradictory versions of the same claim;
- the best passage when many near-duplicates exist.

## Verdict

**Document infrastructure: strong.**
**Real owner-facing recall and answer quality: promising but not yet proven.**

## Required next work

Create a private owner-corpus acceptance harness, without exposing private
bodies in public evidence.

It should include at least these dimensions:

- literal phrase;
- paraphrase;
- synonym;
- cross-language and cross-script;
- approximate date;
- filename and filename alias;
- MIME or file type;
- document revision;
- table cell;
- heading and section;
- scanned/OCR material where supported;
- document plus conversation;
- document plus current web;
- conflicting sources;
- ambiguous query;
- absence case.

Measure two separate things:

1. **retrieval quality**
   - recall@5;
   - recall@10;
   - MRR;
   - authorized-source precision;
   - stale-revision rejection;

2. **answer quality**
   - whether the final answer uses the correct passage;
   - whether every material claim is supported;
   - whether citations identify the right source and passage;
   - whether missing coverage is stated honestly;
   - whether the model avoids substituting a plausible but wrong document.

Do not publish private qrels or bodies. Public evidence should contain only
closed case identities, outcome classes, counts, digests, release identity, and
test node IDs.

---

# 4.3 Second brain and Semantic Supervisor

## Strong

The second-brain boundary is exceptionally disciplined.

The current design preserves:

- one accepted GPT-OSS profile;
- bounded typed requests and typed outputs;
- explicit workload policy;
- no tools for the secondary;
- no effects for the secondary;
- no publication authority for the secondary;
- no independent lifecycle authority;
- primary-only behavior when the laptop is unavailable;
- exact profile, gateway, CA, and release binding;
- restart-aware evidence and observation;
- strict current-file/public-web topic isolation.

The current-file-plus-web query extractor is particularly valuable: private
file bytes, filenames, and deictics are not exported as public search queries.

## Weak

At the reviewed snapshot, the Supervisor runtime still described itself as a
discarded advisory shadow. Source `0.208.4` is an assist candidate, not proof of
a deployed useful assist journey.

The canonical `current_file_web_comparison` journey remained `UNVERIFIED`.

The current architecture also places the Supervisor outside the primary routing
decision and starts secondary work after the primary object exists. That is safe,
but it risks limiting the second brain to a post-hoc reviewer. Grok must verify
what assist actually changes after promotion:

- source plan;
- contradiction detection;
- missing-evidence detection;
- final answer synthesis;
- or only a late advisory annotation.

One successful activation witness proves that the path can be consumed. It does
not prove that the second brain improves representative user work.

## Verdict

**Safety and authority design: excellent.**
**Demonstrated product utility: incomplete until S3 is activated and measured.**

## Required S3 completion

Finish the already active S3 work without broadening its authority.

After genuine activation:

1. verify exact primary-only fallback with the laptop unavailable;
2. verify timeout, malformed JSON, profile drift, CA drift, and restart;
3. run a representative private shadow/assist evaluation window;
4. compare primary-only and assisted outcomes on:
   - document mapping;
   - current-file-plus-web comparison;
   - contradiction detection;
   - missing evidence;
   - latency;
   - failure rate;
5. record whether the advice was actually consumed;
6. keep one primary tool/effect/publication owner;
7. roll back immediately on non-trivial quality regression.

Do not expand the secondary into coding, research planning, or Engineer review
until the existing S3 journey demonstrates non-regressive value.

Later, the second brain may support:

- bounded repository map;
- architecture-plan critique;
- test-plan critique;
- web coverage critique;
- contradiction review.

It must still never become an independent shell actor or publisher.

---

# 4.4 Multi-step work, continuation, and recovery

## Strong

Friday now has several mature durable continuations:

- EngineerWorkItem v1;
- exact source slots;
- command ledger and command fences;
- bounded replan;
- restart-safe terminal receipt recovery;
- exact `RUNNING` and `UNKNOWN` behavior;
- final CAS publication;
- reminder and mission recovery;
- archive selection and replay;
- compare-current-file/web work state;
- exact message continuation;
- bitemporal and graph continuation;
- one-turn authenticated authority across text, files, attachments, and V12.

The generic-continuation fence added after `0.208.1` is a meaningful integrity
improvement.

## Weak

The system achieves coherence through many journey-specific implementations:

- Engineer work items;
- archive candidate work items;
- archive evidence work items;
- conversation/document comparison stores;
- current-file/web work graphs;
- mission and reminder stores;
- secondary scheduler state;
- Supervisor observations;
- Telegram notification ledgers.

This is not automatically wrong. The risk is that each journey develops its own:

- plan representation;
- stage vocabulary;
- progress messages;
- retry interpretation;
- completion state;
- output packaging;
- recovery narrative.

Friday has a strong one-turn nervous system, but it does not yet have one
uniform **operation view** spanning all those durable workers.

The primary brain can therefore be correctly authorized while still receiving
an incomplete picture of what the rest of the organism is doing.

## Verdict

**Durable mechanics: strong.**
**Shared operational awareness and user-visible continuity: medium.**

## Required architectural improvement

Introduce a small, read-only, code-owned **Shared Operation View**.

It must not become a new execution owner or generic WorkGraph.

Preferred shape:

```text
SharedOperationViewV1
  operation_id
  authenticated_turn_id
  mode
  owner / conversation binding digest
  authorized source summary
  ordered plan projection
  active step
  durable pending-work owner
  inherited deadline
  capability availability
  secondary availability
  produced artifacts summary
  terminal evidence summary
```

Derive it from existing authoritative stores and current turn authority.

Provide two bounded projections from the same source:

1. `AgentSituationProjectionV1`
   - for the primary model;
   - a smaller advisory subset for the secondary;
   - no secrets, private paths, bodies, or authority tokens;

2. `OperationProgressProjectionV1`
   - for Telegram and other user-facing transports;
   - no execution authority;
   - no model-authored success claims.

This is the seam that should unite the organism without replacing its organs.

---

# 4.5 General integrity and coherence

## Strong

Friday has unusually strong system-level invariants for a personal agent:

- source identity is code-owned;
- authority is checked repeatedly;
- effects are fenced;
- final publication is singular;
- restarts are treated explicitly;
- ambiguity can remain `UNKNOWN`;
- secondary absence does not change ownership;
- exact releases, fallbacks, and recovery evidence exist;
- old dialogue search tools were retired without deleting internal compatibility;
- model-visible and internal capability surfaces are separated.

These properties matter more than a larger model.

## Weak

The implementation surface is very large and concentrated.

At the reviewed head:

- `friday/agent_runtime/__init__.py` was approximately 3.81 MB;
- Telegram transport and command modules are each large;
- web search is concentrated in a large module;
- interaction-control-plane contains many large journey-specific contracts and
  stores.

This creates:

- merge-conflict concentration;
- poor local comprehensibility for both humans and models;
- hidden coupling;
- expensive review;
- a higher chance that a new mode bypasses an existing invariant;
- repeated test and evidence ceremonies;
- pressure to add one more special case to a giant runtime file.

## Required maintainability ratchet

Do not begin a broad rewrite.

Apply these rules to all new work:

- no new product logic directly inside the giant agent-runtime module unless no
  narrow seam exists;
- implement new behavior in bounded modules;
- keep integration changes thin;
- extract a touched seam only with exact parity tests;
- do not rename or reshuffle unrelated code inside a feature release;
- each new module must state its authority owner, state owner, effect owner,
  publisher, deadline owner, and recovery behavior;
- each new persistent state field must have backup, restore, migration, and
  deletion semantics;
- each new user journey must consume the existing Shared Operation View rather
  than create another progress system.

A gradual seam extraction is justified. A big-bang "clean architecture" rewrite
is not.

---

# 5. Product scorecard at the reviewed snapshot

| Area | Score | Comment |
|---|---:|---|
| Authority, provenance, and reauthorization | 9.5/10 | The strongest part of the system |
| Exactly-once effects and publication | 9/10 | Strong, with explicit ambiguity handling |
| Restart and durable continuation | 8.5/10 | Mature across several important journeys |
| Document ingestion and passage projection | 9/10 | Converged and well evidenced |
| Document retrieval architecture | 8.5/10 | One facade, dense + lexical + exact lanes |
| Real owner-corpus document answer quality | 7/10 | Strong synthetic signal, weak live proof |
| Conversation recall | 7/10 | Exact windows strong, semantic/cross-lane gaps remain |
| Second-brain safety | 9/10 | Excellent authority boundary |
| Second-brain proven utility | 6/10 | S3 not yet fully activated and measured |
| Multi-step model awareness | 7/10 | Durable state exists, shared situation view does not |
| User-visible progress and notification coherence | 4.5/10 | Good primitive, fragmented product behavior |
| Web search provider engineering | 7/10 | Strong safety and refusal semantics |
| Gemini-class web research workflow | 5.5/10 | Search exists; research planning and synthesis remain shallow |
| Maintainability and local comprehensibility | 4.5/10 | Large monoliths and many journey-specific stores |
| Foundation for future modes | 8.5/10 | Ready for careful product expansion |

---

# 6. Mandatory independent re-audit by Grok

Do not copy these conclusions into the backlog as unquestioned truth.

Perform your own audit over the actual current `main` and live installation.
Reconcile differences explicitly.

## 6.1 Baseline verification

At minimum:

```bash
git fetch --all --prune
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
git worktree list --porcelain
git branch --all --sort=-committerdate
git log --all --decorate --oneline --date-order -n 200
```

Verify:

- current source version;
- current production version;
- deployed commit/tree/wheel/schema;
- predecessor and fallback;
- active activation or release journals;
- backend and Telegram bridge;
- V12 identity and 40K lease;
- secondary gateway and profile;
- S3 requested/effective mode;
- pending candidate releases;
- dirty worktrees and unintegrated branches.

## 6.2 Focused code and test re-audit

Inspect and execute the current equivalents of:

```bash
pytest -q \
  tests/retrieval_benchmark/test_cutover_readiness.py \
  tests/retrieval_benchmark/test_cutover_readiness_adversarial.py \
  tests/test_memory_exact_internal.py \
  tests/test_memory_exact_internal_adversarial.py \
  tests/test_archive_search_composite_contract.py \
  tests/test_archive_search_exact_dispatch.py \
  tests/test_archive_search_model_discovery.py \
  tests/test_archive_search_runtime_publication.py
```

Also inspect and execute relevant current tests for:

```text
SemanticSupervisorShadowRuntime
current-file plus public-web query extraction
secondary timeout and primary-only fallback
EngineerWorkItem restart and bounded replan
TelegramStatusMessageManager
Engineer progress and terminal notification delivery
notification send/edit ambiguity fences
generated-file and archive publication
```

Discover exact node IDs from the current source. Do not rely on names in this
snapshot if the repository has moved.

## 6.3 Live journey audit

Before declaring the next baseline stable, run benign owner-authorized journeys:

1. locate and answer from one archived document;
2. locate one document by approximate date or alias;
3. retrieve an exact conversation window;
4. combine a current file with an independent current web topic;
5. run a two-step Engineer task across a restart-safe boundary;
6. turn off or isolate the secondary and prove unchanged primary fallback;
7. observe all Telegram messages emitted by one ordinary file task;
8. observe all Telegram messages emitted by one Engineer task;
9. list exact final artifacts and verify that no internal junk is delivered.

Record actual message counts, not only backend receipts.

## 6.4 Audit report handling

Write findings into the existing canonical backlog as bounded new packages.

Do not create a competing audit register.

Update the `Ctrl+T` checklist immediately after backlog reconciliation.

Then implement the accepted backlog. Do not stop after writing the audit.

---

# 7. New owner requirement A
# Universal operation notification and two-message UX

This is a mandatory cross-Friday product contract.

## 7.1 Owner-visible rule

For every interactive user-initiated operation:

```text
one user message
→ one editable Friday status message
→ one final Friday result message
```

Progress updates edit the existing status message. They never create new
progress messages.

The final result is exactly one Telegram carrier:

- one text message when no artifact is needed;
- one file/document message with a concise caption when one artifact exists;
- one deterministic archive with a concise caption when several artifacts are
  required;
- one report file with a concise caption when the final text cannot fit one
  Telegram message.

There is no separate third "completed" message.

The status message is edited to its terminal state after final delivery is
confirmed. If delivery is uncertain, the status message must say so and the
system must not blindly duplicate the result.

## 7.2 Scope

Apply this contract to:

- ordinary `/chat`;
- current uploads;
- archive and document work;
- document map;
- archive search;
- web research;
- file plus web;
- `/engineer`;
- future `/coding`;
- interactive mission operations;
- any other interactive path that currently emits intermediate messages.

Scheduled reminders and genuinely future asynchronous events are separate
accepted operations. They do not have to be forced into the original turn's
two-message window, but each emitted interactive operation must still use one
coherent publication contract.

## 7.3 Required status presentation

For a task with two or more meaningful steps, show the ordered plan.

Preferred Russian rendering:

```text
⏳ Выполняю задачу

✅ Обрабатываю «report.pdf» - 100%
▶️ **Ищу «contract.docx» - 2 из 4 источников**
▫️ Сопоставляю документы - 0%
▫️ Ищу актуальные данные в интернете - 0%
▫️ Формирую сводную таблицу - 0%

Прошло: 1 мин 24 с
```

Engineer example:

```text
⏳ Проверяю сеть 192.168.1.0/24

✅ Обнаруживаю активные хосты - 100%
✅ Определяю сервисы и версии - 100%
▶️ **Сопоставляю сервисы с кандидатами уязвимостей - 18 из 31**
▫️ Безопасно проверяю применимость - 0%
▫️ Формирую отчёт - 0%

Прошло: 6 мин 12 с
```

Coding example:

```text
🛠 Создаю проект «inventory-api»

✅ Анализирую требования - 100%
✅ Создаю структуру проекта - 100%
▶️ **Реализую API - 7 из 12 задач, 58%**
▫️ Запускаю тесты и исправляю ошибки - 0%
▫️ Готовлю исходники и инструкцию - 0%
```

Use:

- `✅` for complete;
- `▶️` and bold text for the current focus;
- `▫️` for pending;
- `⚠️` for blocked or uncertain;
- `❌` for failed;
- `⏹` for cancelled.

There must be at most one primary current-focus step. Parallel background work
may be shown as a bounded subordinate fact, not as several competing "current"
steps.

## 7.4 Truthful progress only

Never invent percentages or ETAs.

Allowed percentage sources:

- completed fixed plan units;
- files processed out of a known total;
- tests completed out of a known total;
- hosts scanned out of a fixed target set;
- build stages completed out of an accepted plan;
- bytes processed when total bytes are known;
- a tool's authenticated native progress.

For an unmeasurable step:

- show no percentage;
- show a measured count;
- or show only the state.

A completed step may show `100%`. A pending step may show `0%`. A running
open-ended reasoning step must not display a fabricated `63%`.

A hard timeout may be shown. A guessed completion time may not.

## 7.5 Plan evolution

The operation plan may change when evidence requires replanning.

Rules:

- preserve completed and failed steps;
- never silently remove a problem from the display;
- show `План уточнён` once when the plan changes materially;
- keep revisions monotonic;
- retain one active operation identity;
- do not create a new status message after restart;
- do not let model-authored prose claim completion without terminal evidence.

## 7.6 Architecture

Reuse the existing durable `TelegramStatusMessageManager` behavior:

- one known message coordinate;
- monotonic revisions;
- absorbing terminal state;
- send/edit ambiguity fence;
- restart-safe replacement behavior;
- advisory status failure that cannot duplicate execution.

Build a shared operation-progress projection above it.

Do not create a second execution engine.

Preferred contract:

```text
OperationProgressProjectionV1
  operation_id
  authenticated_turn_id
  revision
  terminal
  ordered_steps[]
  active_step_id
  measured_facts
  elapsed_sec
  hard_deadline_remaining_sec?
  result_delivery_state
```

Each step should have:

```text
step_id
safe_label
state
completed_units?
total_units?
percentage?          # only when derivable
evidence_class
```

Status text must be rendered by code-owned templates.

Model-proposed plan labels may be used only after:

- length limits;
- character validation;
- Telegram escaping;
- filename escaping;
- secret and path screening;
- private-chat binding.

Do not persist arbitrary status prose when the same view can be regenerated from
durable facts.

If a small persistent projection is required, it owns presentation only. It must
not become an effect owner, task authority, scheduler, or new generic WorkGraph.

## 7.7 Engineer output cleanup

Audit and replace the current fragmented Engineer delivery behavior.

Required rules:

- no separate progress messages;
- no separate terminal-status message;
- no empty archive;
- no archive for one ordinary output file;
- no internal receipts, logs, temporary files, stdout dumps, manifests, or
  caches in the user archive unless explicitly requested or necessary to
  understand the result;
- every archive has a meaningful name;
- every archive has a user-facing manifest describing only delivered files;
- result text and artifact are one final carrier where Telegram allows it;
- several requested artifacts become one deterministic archive;
- console-only result becomes one final text message;
- large report becomes one report document with a concise caption;
- delivery uncertainty edits the status and never causes a blind duplicate.

The final result carrier must be bound to the same authenticated operation and
published exactly once.

## 7.8 Acceptance journeys

At minimum:

1. simple chat, no tools;
2. one uploaded file;
3. two uploaded files plus archive lookup plus public web plus table;
4. document-map task;
5. ordinary web research;
6. two-step Engineer command;
7. long Engineer task across restart;
8. Engineer result with no files;
9. Engineer result with one file;
10. Engineer result with several files;
11. final Telegram send accepted but response lost;
12. edit rejected and safely replaced;
13. status edit transport failure;
14. cancel;
15. timeout;
16. honest `UNKNOWN`;
17. duplicate inbound update;
18. model tries to imitate terminal text;
19. plan changes after failed hypothesis;
20. final text exceeds Telegram limit.

For each journey assert:

- exactly one status message is created;
- all progress uses edits;
- exactly one final result carrier is created;
- execution occurs exactly once;
- final publication occurs exactly once;
- restart does not create a second status;
- no user-visible internal junk is delivered.

---

# 8. New owner requirement B
# Coding Mode

Implement a new owner-facing mode analogous in autonomy to Engineer Mode but
specialized for software creation and modification.

Preferred command and product name:

```text
/coding
coding_mode
```

## 8.1 User journeys

### Prompt to application

The owner describes an application.

Friday must:

1. understand and normalize the request;
2. create a bounded implementation plan;
3. publish that plan in the universal editable status message;
4. create the project;
5. implement it;
6. build, lint, and test it where applicable;
7. diagnose and repair bounded failures;
8. package the result;
9. send one final summary and source artifact carrier;
10. remember the accepted project state for future continuation.

### Modify uploaded project

The owner uploads one or more source files or a source archive and asks for a
change.

Friday must:

1. authenticate and snapshot the exact input;
2. unpack it safely;
3. understand the project;
4. create a baseline revision;
5. plan the requested change;
6. edit the project;
7. build/test it in an isolated worker;
8. package the modified source;
9. publish one final carrier;
10. preserve the new accepted revision.

### Bare source upload

When the owner sends source files or an archive without an implementation
request:

- inspect;
- identify language, structure, dependencies, and likely entry points;
- summarize;
- report risks or obvious defects;
- do not execute untrusted code by default;
- do not modify the project;
- do not publish a rebuilt archive unless asked.

### Continue prior work

The owner may ask Friday to continue or modify a previously created project.

Friday must resolve exactly one project identity from:

- explicit project ID or name;
- the current conversation's last accepted coding project;
- a direct reply to the prior final result.

Ambiguity must produce one concise clarification. It must never select an
unrelated project by recency alone.

## 8.2 Persistent project state

Model memory is not the source of truth.

Each coding project must have a code-owned project identity and durable state on
disk.

Preferred source of truth:

- a private project directory;
- a Git repository or equally strong immutable snapshot history;
- a small canonical project manifest;
- exact input and revision digests;
- current accepted revision;
- language and toolchain facts;
- build/test commands;
- user-visible project name;
- last successful validation;
- known limitations;
- produced artifact identity.

Do not store chain of thought.

Do not store credentials in the project manifest.

Do not make arbitrary working-tree recency the project selector.

A minimal SQLite index may map owner, conversation, project ID, and accepted
revision if needed. The actual source remains on disk under exact project
identity.

## 8.3 Workspace and security boundary

Uploaded source code is untrusted.

Do not execute arbitrary project code, dependency installers, build scripts,
package hooks, tests, binaries, emulators, or generated applications inside the
primary Friday production trust boundary.

Coding Mode needs a dedicated execution boundary, preferably:

- a disposable Coding Worker VM;
- or a disposable strongly isolated container/namespace with no host secrets,
  no Docker socket, no production database, no owner SSH keys, and bounded
  network;
- one project workspace per operation;
- resource and time limits;
- controlled dependency cache;
- network disabled by default and enabled only for declared dependency/research
  steps;
- immutable input snapshot;
- deterministic export path.

Until this boundary exists, Coding Mode may provide static inspection and source
editing, but must not claim safe build/test execution of untrusted uploads.

Do not weaken ordinary Engineer Mode or primary release certification to create
this worker.

## 8.4 Safe archive handling

Before extraction:

- enforce compressed and uncompressed size limits;
- reject traversal;
- reject absolute paths;
- reject device files;
- reject unsafe symlinks and hardlinks;
- bound file count and nesting;
- detect archive bombs;
- preserve exact input digest;
- do not overwrite another project;
- do not allow case-folding collisions;
- do not trust executable permission bits as authority.

## 8.5 Implementation loop

Use a bounded coding lifecycle:

```text
inspect
→ plan
→ scaffold or unpack
→ implement
→ lint/build/test
→ diagnose
→ bounded replan and repair
→ acceptance
→ package
→ publish
```

Do not prebuild a huge compiler catalogue.

Discover the project toolchain and use demand-driven adapters. Add a specialized
adapter only after a real project proves the general command/tool path
insufficient.

The primary remains the only effect owner.

The second brain may later provide bounded:

- repository map;
- architecture critique;
- test-plan critique;
- patch review.

It must not run commands or publish results.

## 8.6 Output contract

The final Coding Mode result is one carrier.

When an artifact is required, deliver:

- one source archive;
- a concise Russian caption;
- deterministic name;
- source manifest;
- README or build instructions;
- test/build summary;
- exact revision and checksum;
- known limitations.

Exclude by default:

- `.git`;
- virtual environments;
- downloaded dependency caches;
- `node_modules`;
- build caches;
- temporary files;
- secrets;
- credentials;
- local machine paths;
- internal Friday receipts;
- model transcripts;
- chain of thought.

Include compiled binaries only when requested and when their provenance and
validation are clear.

## 8.7 Coding status example

Use the universal progress contract:

```text
🛠 Дорабатываю проект «photo-indexer»

✅ Проверяю исходный архив - 100%
✅ Строю карту проекта - 100%
✅ Планирую изменения - 100%
▶️ **Реализую импорт метаданных - 9 из 14 задач, 64%**
▫️ Запускаю тесты - 0%
▫️ Исправляю найденные ошибки - 0%
▫️ Готовлю исходники и инструкцию - 0%
```

Percentages must be measured, not narrated.

## 8.8 Coding Mode acceptance journeys

At minimum:

1. prompt creates a small application that passes its tests;
2. prompt creates a small CLI;
3. uploaded repository receives a bounded feature;
4. uploaded repository receives a bug fix;
5. bare archive produces summary only;
6. restart during build resumes without duplicate effects;
7. failed build triggers bounded diagnosis and repair;
8. irreparable build yields an honest partial result;
9. malicious traversal archive is rejected;
10. archive bomb is rejected;
11. symlink/hardlink escape is rejected;
12. dependency installer attempts host access and is contained;
13. generated secret is caught before publication;
14. duplicate inbound update creates one revision;
15. multiple output files become one deterministic final archive;
16. follow-up continues the exact accepted project;
17. ambiguous follow-up asks once rather than selecting by recency;
18. laptop/secondary unavailable preserves primary-only coding behavior;
19. web documentation is researched and applied to the implementation;
20. rollback restores the prior accepted project revision.

---

# 9. New owner requirement C
# Web research comparable to a strong consumer research assistant

The owner wants Friday to research the public web at a level approaching a
strong Gemini web experience.

Do not claim parity without a paired benchmark.

The goal is not merely "more search results". The goal is:

```text
recognize a knowledge gap
→ plan research
→ search multiple angles
→ read primary sources
→ reconcile evidence
→ use the evidence inside the requested task
→ cite it correctly
```

## 9.1 Current strengths

Friday already has:

- SSRF-resistant public HTTP(S) access;
- DNS and redirect validation;
- robots handling;
- HTML, text, JSON, XML, and PDF extraction;
- explicit provider refusal semantics;
- freshness and domain filters;
- Yandex, Brave API, Tavily, and Serper adapters when configured;
- weaker HTML fallbacks;
- source-class handling;
- bounded research timing;
- file/public-topic isolation.

The provider engineering is not the main missing piece.

## 9.2 Current weakness

The existing defaults are suitable for a compact answer, not deep research:

- a small result count;
- a small source count;
- one short total research window;
- limited query decomposition;
- limited iterative coverage checking;
- no universal claim ledger consumed by downstream modes.

The repository's own measurements show that free HTML providers degrade badly
under repeated requests. A dependable production research contour needs at least
one reliable primary provider and a genuinely independent fallback. When only
one reliable provider is configured, report resilience as degraded rather than
pretending HTML fallbacks are equivalent.

## 9.3 Automatic research policy

Friday must automatically research when any of these conditions apply:

- the owner explicitly asks to search, verify, compare, or find current data;
- the answer depends on current or recently changeable facts;
- the request contains "latest", "today", "current", a current office holder,
  current price, current software version, law, schedule, news, or product
  availability;
- a specific external page, paper, product, service, standard, or dataset is
  referenced but not present locally;
- the model is unfamiliar with a material term;
- the model lacks sufficient evidence for a material factual claim;
- a coding task depends on current library/API documentation;
- an Engineer task depends on current advisories, package documentation, or
  compatibility information;
- retrieved local sources conflict and public evidence can resolve the issue.

Do not search every timeless question.

The current date and time context must be available to the primary when
freshness is material.

A model may request research, but a code-owned currentness policy may require it
even when the model forgets.

## 9.4 Privacy boundary

Never send private document text, filenames, paths, identifiers, or deictics to
a public provider by default.

Use locally derived public concepts and sealed query intents.

Sending an exact private excerpt to a search provider requires explicit owner
authorization and a visible warning.

## 9.5 Research mission

Implement a bounded multi-query research mission:

1. classify the question and freshness;
2. create 2-8 complementary public queries;
3. select source classes and filters;
4. search through reliable providers;
5. fetch and parse the most useful sources;
6. prefer primary and authoritative sources;
7. diversify domains;
8. identify duplicate reporting;
9. extract claims with source and retrieval time;
10. detect disagreement;
11. score coverage;
12. issue one or two follow-up query rounds if coverage is insufficient;
13. produce a bounded `WebEvidenceBundleV1`;
14. make the requesting workflow consume that bundle;
15. publish citations in the final result.

The primary remains the only tool caller and publisher.

The second brain may later critique query coverage or contradictions, but cannot
independently browse or publish.

## 9.6 Evidence bundle

Preferred shape:

```text
WebEvidenceBundleV1
  research_id
  authenticated_turn_id
  task_topic
  freshness_requirement
  query_plan[]
  sources[]
  claims[]
  contradictions[]
  missing_evidence[]
  coverage
  retrieved_at
  provider_outcomes
```

Each source should include:

- canonical public URL;
- title;
- publisher/domain;
- publication or update date when proven;
- retrieval time;
- source class;
- content digest;
- relevant passage references.

Each claim should include:

- normalized claim;
- supporting source IDs;
- contradicting source IDs;
- confidence/evidence state;
- whether the claim is current-sensitive.

The bundle must be used by downstream synthesis. It is not a decorative search
report.

## 9.7 Integration into real work

Web evidence must be usable inside:

- ordinary answers;
- document comparison;
- table generation;
- Coding Mode;
- Engineer Mode;
- dependency/API selection;
- current vulnerability/advisory analysis on authorized targets;
- research reports;
- future long-running missions.

Example owner journey:

```text
read two local files
→ find current public information about their subject
→ reconcile local and current evidence
→ create a summary table
→ deliver one final result
```

The operation status must show each phase in the same editable message.

## 9.8 Research quality benchmark

Create a private representative benchmark and a body-free public summary.

Measure:

- query coverage;
- source diversity;
- authoritative-source rate;
- freshness correctness;
- claim support;
- citation entailment;
- contradiction detection;
- unsupported-claim rate;
- final task utility;
- latency;
- provider failure recovery;
- primary-only fallback;
- private-data leakage.

Include:

- current news;
- current software/API documentation;
- product comparison;
- legal or regulatory update;
- scientific topic;
- niche technical topic;
- current company/person role;
- a document plus web task;
- a coding task requiring current docs;
- an authorized engineering task requiring current advisories.

A manual paired comparison with Gemini may be recorded on the same owner-curated
questions. Do not claim "no worse than Gemini" until the evaluation method,
questions, dates, source access, and scoring are fixed in advance.

---

# 10. New cross-cutting package
# Whole-organism awareness and acceptance

After the three owner requirements are implemented, audit the system as one
organism.

## 10.1 Brain awareness

Verify that the primary receives a bounded, coherent view of:

- active mode;
- current operation;
- current plan;
- current step;
- authorized sources;
- exact current files;
- selected archive evidence;
- pending durable work;
- available tools;
- unavailable capabilities;
- secondary availability;
- inherited deadline;
- expected output;
- terminal evidence.

Verify that the secondary receives the same operation identity and only the
advisory subset it needs.

No brain should infer critical organism state from old assistant prose.

## 10.2 Cross-lane journeys

Create end-to-end journeys that cross multiple organs:

1. current file + archived file + conversation + public web + table;
2. document search + exact passage + follow-up after restart;
3. Engineer scan + current advisories + verified report;
4. Coding project + current documentation + build/test + archive;
5. secondary unavailable during document task;
6. web provider refusal during mixed-source task;
7. restart during status editing and durable execution;
8. final delivery accepted but acknowledgement lost;
9. source revoked before final publication;
10. plan changes after a failed first hypothesis.

For each, prove:

- one turn identity;
- one operation identity;
- one status message;
- one final result carrier;
- one effect owner;
- one final publisher;
- no private source leakage;
- no duplicate execution;
- no duplicate delivery;
- truthful partial or `UNKNOWN`;
- restart recovery;
- rollback where an effect exists.

## 10.3 Definition of a coherent Friday

Friday is coherent when:

- the primary and secondary understand the same accepted operation;
- every tool result returns to the same operation identity;
- durable workers expose current state through the Shared Operation View;
- user-visible status is a projection of real state;
- final publication is bound to terminal evidence;
- missing capability changes quality, not ownership;
- a restart does not change the meaning of the task;
- the user does not have to reconstruct one operation from ten messages;
- a new mode composes existing primitives rather than bypassing them.

---

# 11. Required implementation order

Use this order unless the independent re-audit finds a concrete blocker.

## Phase N0 - Reconcile and stabilize the current baseline

1. finish the active S3 candidate;
2. reconcile `main` versus production;
3. independently validate the candidate;
4. deploy or explicitly supersede it;
5. update canonical production identity;
6. freeze one clean baseline.

Do not bundle the new notification system into the S3 release.

## Phase N1 - Universal operation progress and two-message publication

This is the first new product package.

Reason:

- all later modes require it;
- it exposes actual system coherence;
- it removes the current Engineer message flood;
- it provides the user-visible skeleton for Coding Mode and deep research.

Ship in small slices:

1. shared progress contract and renderer;
2. ordinary chat and files;
3. archive/document/web paths;
4. Engineer migration;
5. final artifact publication convergence;
6. restart and delivery-ambiguity acceptance;
7. full cross-lane release.

Do not declare completion until actual Telegram message counts are proven.

## Phase N2 - Web research mission and automatic research policy

Build on the unified operation view and status.

Ship:

1. knowledge-gap/currentness policy;
2. multi-query planner;
3. provider resilience and source diversity;
4. `WebEvidenceBundleV1`;
5. iterative coverage and contradiction checks;
6. downstream consumption;
7. owner benchmark and live acceptance.

## Phase N3 - Coding Mode

Build on:

- authenticated turn authority;
- unified progress;
- web evidence;
- durable project state;
- isolated coding worker;
- existing Engineer execution primitives;
- one final publisher.

Ship an MVP first:

1. bare archive inspection;
2. prompt-to-small-project;
3. uploaded-project modification;
4. build/test in isolation;
5. persistent continuation;
6. deterministic source archive;
7. restart and adversarial acceptance.

Do not begin with universal language support.

## Phase N4 - Whole-organism audit and golden journeys

Run the cross-lane battery, close remaining product gaps, and update the
canonical journey registry.

## Phase N5 - Gradual maintainability convergence

Only after behavior is stable:

- extract touched seams from giant runtime modules;
- reduce duplicate stage and publication logic;
- delete superseded adapters only with parity proof;
- preserve public and internal contracts;
- keep releases reversible.

---

# 12. Suggested canonical backlog structure

Append the following bounded packages to
`outer_sol/PROJECT_BACKLOG.md` after the independent audit. Adapt exact IDs to
the current repository, but do not change their intent.

```text
N0 - Post-backlog baseline reconciliation
  - reconcile source candidate, production, fallback, S3, evidence, and journals
  - certify and deploy or explicitly supersede the current candidate chain

N1 - Universal Operation Progress and Two-Message UX
  - one editable status plus one final result for every interactive operation
  - unified plan/step projection
  - truthful measured progress
  - Engineer notification and artifact cleanup
  - restart and ambiguous-delivery proof

N2 - Deep Web Research and Automatic Knowledge-Gap Search
  - currentness policy
  - multi-query research mission
  - reliable provider fallback
  - source and claim evidence bundle
  - downstream task consumption
  - citation and quality benchmark

N3 - Coding Mode
  - prompt-to-project
  - uploaded-project modification
  - bare-source inspection
  - persistent project identity and revisions
  - isolated build/test worker
  - deterministic final source archive
  - restart, rollback, and adversarial acceptance

N4 - Whole-Organism Coherence
  - shared operation/situation projection
  - primary/secondary state agreement
  - mixed document/conversation/web/Engineer/Coding journeys
  - one effect owner, one publisher, one status, one result

N5 - Maintainability Ratchet
  - no new logic in monolithic runtime without a narrow seam
  - extract only touched behavior with parity tests
  - retire duplicate stages and publishers after measured equivalence
```

Keep existing owner-parked items parked unless the owner explicitly activates
them:

- destructive P0H deletion;
- physical Android/Obsidian acceptance;
- off-machine backup destination;
- provider-side credential rotation.

S3 is not owner-parked while the current activation is actively being completed.
Finish it as its own small release boundary.

---

# 13. `Ctrl+T` checklist to establish after re-audit

Use a compact executable checklist resembling:

```text
▶ Reconcile main / production / S3 candidate and freeze baseline
□ Publish independent post-backlog audit findings into canonical backlog
□ Implement universal operation progress contract
□ Migrate /chat, files, archive, and web to one editable status
□ Migrate /engineer and remove message/archive flood
□ Certify two-message UX across restart and delivery ambiguity
□ Implement automatic currentness and knowledge-gap web policy
□ Implement multi-query WebEvidenceBundle research mission
□ Integrate web evidence into document, Engineer, and table workflows
□ Implement isolated Coding Mode foundation
□ Ship prompt-to-project and uploaded-project modification
□ Add persistent project continuation and deterministic source delivery
□ Run whole-organism cross-lane acceptance
□ Perform final exact-release certification, deploy, and update backlog
```

Show delegated or observation-bound work separately. Do not keep completed items
as an ever-growing wall in the active checklist.

---

# 14. Release strategy

Do not release all new requirements as one giant version.

Recommended boundaries:

1. current S3/baseline release;
2. progress contract foundation;
3. ordinary chat/files migration;
4. Engineer notification convergence;
5. research policy and evidence bundle;
6. research task integration;
7. Coding Mode static inspection and project persistence;
8. isolated build/test;
9. Coding Mode full MVP;
10. cross-lane coherence release.

Every release must be independently reversible.

A release that changes notification transport must prove it cannot duplicate the
underlying task.

A release that changes final artifact delivery must prove exactly-once
publication and uncertain-send behavior.

A release that executes uploaded code must prove isolation before activation.

A release that activates secondary advice must preserve primary-only fallback.

---

# 15. Definition of done for the new phase

The new phase is complete only when all of these are true:

1. source, production, fallback, and S3 states are reconciled;
2. the canonical backlog contains the new packages and no competing register;
3. the `Ctrl+T` checklist is current;
4. every interactive Telegram task creates one status message;
5. every progress update edits that message;
6. every task creates at most one final result carrier;
7. Engineer no longer floods the chat with terminal/progress/archive messages;
8. delivered archives contain only meaningful user-facing output;
9. current and uncertain facts trigger research under a measured policy;
10. research uses several queries and sources when the task requires it;
11. web evidence is consumed by the requested task;
12. Coding Mode creates and modifies real projects;
13. Coding projects survive restart and continue from exact accepted state;
14. untrusted code executes only in the dedicated coding boundary;
15. primary and secondary share one operation identity and coherent situation;
16. secondary remains advisory and primary-only fallback is intact;
17. mixed document, conversation, web, Engineer, and Coding journeys pass;
18. full exact-release certification passes;
19. immutable production activation and rollback pass;
20. the canonical backlog records the exact deployed identity and honest
    remaining owner/external rows.

---

# 16. First action for Grok

1. Re-read the permanent succession directive.
2. Perform the independent audit in Section 6.
3. Reconcile the actual current candidate and S3 state.
4. Send the owner one concise Russian checkpoint with:
   - current `main`;
   - current production;
   - S3 state;
   - material differences from this audit;
   - the exact proposed backlog packages;
   - the first implementation item.
5. Update only `outer_sol/PROJECT_BACKLOG.md`.
6. Refresh the `Ctrl+T` checklist.
7. Begin Phase N0, then proceed through N1-N4.
8. Report only blockers, important checkpoints, releases, rollback, and final
   completion.

Do not stop after producing another audit. Turn the reconciled findings and the
three owner requirements into the canonical backlog, construct the executable
todo list, and implement them through small reversible releases.
