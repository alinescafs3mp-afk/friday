# Friday Project Convergence: Architect Implementation Brief

> Document ID: FRIDAY-CONVERGENCE-001  
> Status: External architecture handoff, draft v0.1  
> Date: 23 August 2026  
> Observed repository checkpoint: `main` at `7ebe015c2ba74422d0e4605ab87e188041ab0e73`  
> Observed production checkpoint: Friday `0.207.4` / `4c02ab8e3bbfac4f56d9e838dd016afb7c55711e`, schema 37  
> Audience: Friday system architect and implementation lead  
> Scope: product convergence, golden user journeys, retrieval identity, durable work state, effect outcomes, release evidence, recovery, and physical last-mile certification  
> Related documents: [`PROJECT_IMPLEMENTATION_STATUS.md`](PROJECT_IMPLEMENTATION_STATUS.md), [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md), [`INTERACTION_CONTROL_PLANE_IMPLEMENTATION_STATUS.md`](INTERACTION_CONTROL_PLANE_IMPLEMENTATION_STATUS.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), [`V12_FURTHER_REFINEMENT_STATUS.md`](V12_FURTHER_REFINEMENT_STATUS.md), [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md), and [`SYSTEM_ASSURANCE_AND_RECORDS_GOVERNANCE.md`](SYSTEM_ASSURANCE_AND_RECORDS_GOVERNANCE.md).

## How to use this brief

This document is not a request to implement every item in one branch or one release.

Before acting, re-read the canonical status register and current code. The checkpoint above records the state observed while this brief was written. If `main` or the live release has advanced, the current canonical status and verified production identity take precedence.

The architect is expected to challenge details that no longer fit the code, preserve proven contracts, and convert the direction below into small independently releasable packages. Do not treat this text as permission to widen scope, weaken a gate, inspect live user content, or replace an existing safe mechanism without demonstrated benefit.

## Operator objective

The operator is no longer asking primarily for more isolated capabilities.

The objective is a Friday that feels like one assembled and dependable product:

```text
it understands the current goal
it finds the correct material
it keeps the selected material and task state across turns and restarts
it distinguishes partial evidence from completion
it performs an authorized action at most once
it can prove what happened
it degrades honestly
it can recover
```

Document and message recall remain especially important. Friday must be able to register, classify, find, read, discuss, and cite documents, including searches by approximate content or approximate date. Conversation history must be similarly discoverable with meaningful adjacent context. A larger model may improve local decisions, but it cannot substitute for missing source identity, coverage semantics, durable work state, idempotency, or recovery evidence.

## Executive architectural judgement

The current direction is correct.

Friday already has unusually strong foundations:

- immutable and privacy-safe turn tracing;
- durable precommit failure traces;
- typed read outcomes;
- deterministic completion gates;
- atomic accepted-result receipts;
- source and live release identity;
- immutable wheel-only production activation;
- substantial deterministic, integration, synthetic-live, and production checks;
- a mature server-side Obsidian contour;
- an explicitly bounded path toward durable Work Items and Active Frames.

The central risk is no longer lack of capability. The risk is that the project continues to accumulate correct local slices without proving a small number of complete user journeys.

The required shift is:

```text
Old unit of progress:
    another capability or adapter is implemented

New unit of progress:
    one user journey is complete, recoverable, evidence-backed, and honest under degradation
```

The current P2 direction should not be abandoned. The planned schema-38 `RecallConversation` Work Item and bounded Active Frame are a sensible first durable canary. They should become the first product-convergence spine, not an isolated conversation feature and not the beginning of an unrestricted generic WorkGraph.

The recommended order is:

```text
finish the current narrow P2 recall canary
    -> establish product-level golden journeys
    -> establish stable retrieval identities and coverage semantics
    -> expose one read-only archive_search facade
    -> extend the durable recall Work Item across documents and messages
    -> establish one common effect envelope
    -> prove one Obsidian write-and-sync journey
    -> prove clean release, rollback, restore, and physical-device evidence
    -> only then consider generic WorkGraphs or broad effectful expansion
```

## Current checkpoint and immediate implication

At the observed checkpoint, the cumulative non-Docker gate has been recorded as green and P2 has begun in the canonical status register.

The active P2 plan is:

1. a schema-38 foundation for one durable `RecallConversation` Work Item and bounded Active Frame, without runtime behavior in the foundation release;
2. a schema-capable foundation release that can act as rollback-safe fallback;
3. a narrow behavior canary over exact current-conversation time-window reads;
4. a full request creates the Work Item;
5. an immediate closed temporal follow-up such as `А вчера?` changes only the time window while retaining authorized conversation and role;
6. restart, expiry, cancellation, ownership, revision-CAS, receipt, and atomic rollback remain acceptance requirements.

This is compatible with the convergence direction in this brief.

Do not stop or redesign this slice merely to introduce a larger abstraction. Complete it as the first durable golden journey, provided that:

- the schema foundation does not bake prose or model-specific state into the durable contract;
- ownership, authorization, revision, expiry, and cancellation remain first-class;
- accepted outcomes and receipts remain authoritative;
- the Work Item can later refer to stable source and passage identities rather than storing copied evidence as its primary identity;
- no generic candidate-set engine or arbitrary WorkGraph is smuggled into the foundation release.

## What is still missing from a cohesive product

### 1. Product-level completion semantics

The project tracks architectural slices well, but the canonical status still answers this question more clearly:

> Which components have been implemented?

than this question:

> Which user tasks can Friday now be trusted to complete end to end?

The project needs a small canonical registry of golden user journeys. Every journey must have explicit evidence classes and one of these states:

```text
READY
DEGRADED
UNVERIFIED
BLOCKED
OUT_OF_SCOPE
```

At minimum, define these journeys:

#### Document recall and answer

```text
upload or register a document
    -> make it discoverable
    -> find it later by approximate content, title, filename, alias, or date
    -> select the correct source
    -> read the correct passage
    -> answer with a valid citation
    -> accept an immediate follow-up without starting from zero
    -> resume after restart
```

#### Conversation recall

```text
find an older discussion by meaning or time
    -> return meaningful neighboring messages
    -> distinguish message time from dates mentioned in the text
    -> answer with deterministic message citations
    -> preserve the active conversation and role through a short follow-up
    -> resume or expire honestly after restart
```

#### Obsidian write and synchronization

```text
create or mutate a note
    -> record an accepted effect receipt
    -> deliver it through the configured synchronization path
    -> edit it on the actual Android device
    -> ingest the remote change incrementally
    -> avoid duplicate application
    -> preserve both sides of a real conflict
```

#### Durable scheduled work

```text
create a reminder or mission
    -> survive restart
    -> execute or notify at most once
    -> distinguish delivered, uncertain, blocked, and cancelled
    -> expose remaining work and recovery state
```

#### Honest degradation

```text
one provider, index, or source lane is unavailable
    -> preserve successful evidence from other lanes
    -> report partial coverage
    -> never turn incomplete search into a confident absence claim
    -> retain a valid continuation or recovery path
```

A journey is not `READY` because its unit tests pass. Its status must identify which of the following evidence classes exist:

```text
deterministic contract
integration path
clean artifact path
synthetic live path
production read-only observation
physical device evidence
restart and recovery evidence
rollback evidence
backup and restore evidence
```

### 2. Stable retrieval identity

The retrieval audit correctly identifies that registration, admission, indexing, semantic discoverability, and confirmed knowledge are different states.

Friday must not collapse Raw Objects, Inbox records, promoted Knowledge Objects, Obsidian notes, generated files, and conversation messages into one lifecycle table. Their existing semantics are valuable. What is missing is a rebuildable cross-corpus projection and stable logical identity.

Before a broad Work Item or WorkGraph can reliably retain selected evidence, establish these contracts.

#### `SourceRef`

A stable logical source identity that survives movement between lower-level storage representations and does not depend on a display path or copied text.

Minimum semantics:

```text
source_ref
source_type
owner or tenant scope
canonical underlying object identity
lifecycle state
content or revision identity
current authorization target
```

It must be possible to represent at least:

```text
raw or pending document
promoted knowledge object
Obsidian note
conversation
message range
web capture
external registered source
generated artifact
```

A Work Item should retain `SourceRef` values, not a filename alone, a tool-specific row ID without type, or a pasted excerpt as its primary anchor.

#### `CatalogItem`

A rebuildable discoverability projection over existing lifecycle stores.

Candidate fields:

```text
source_ref
source_type
canonical_title
visible_title
filename
aliases
owner and visibility
promotion or review state
ingest state
content hash or revision
created_at
modified_at
received_at
indexed_at
index status and incomplete reason
```

Do not make this projection the source of truth for authorization or lifecycle. It is a search and navigation layer that must be revalidated against the authoritative source before evidence is returned or published.

#### `PassageRef`

An addressable evidence segment:

```text
source_ref
passage_id
source revision
section path or message window
page, line, cell, slide, paragraph, or message locator
citation locator
passage index version
embedding identity and compatibility state
```

The same selected passage must be usable for retrieval, verification, citation, accepted-outcome receipts, and Work Item continuation without silently changing identity.

#### `TemporalFact`

Do not expose one ambiguous `date` field.

Represent typed date roles such as:

```text
document_created_at
document_modified_at
received_at
uploaded_at
indexed_at
event_date
mentioned_date
conversation_time
valid_from
valid_to
```

Approximate-date search must preserve the role requested by the user. No date role may be silently substituted for another.

#### `SearchCoverage`

The result of a search must describe not only candidates but also the scope actually examined.

Minimum states:

```text
complete
partial
stale
unavailable
permission_filtered
backfill_pending
embedding_incompatible
capped
```

This distinction is mandatory:

```text
no matching evidence was found in a complete authorized search
```

versus:

```text
no matching evidence was found in the portion of the authorized archive that was searchable
```

Friday must not claim archive absence while any relevant index is partial, stale, failed, incompatible, or silently capped.

### 3. One high-level read-only retrieval contract

Preserve specialized engines and existing narrow tools, but expose one model-facing logical contract for archive recall.

Suggested name:

```text
archive_search
```

The architect may choose a different internal name if an existing contract is a better fit. The logical behavior matters more than the symbol.

Candidate request shape:

```text
query
source types
title or filename hints
entity hints
temporal constraints with explicit date role
conversation scope
visibility and lifecycle constraints
result limit
continuation token
requested context radius
```

Candidate response shape:

```text
search plan summary
candidates with SourceRef
matching PassageRef values
typed temporal facts
lifecycle and evidence-authority state
coverage by lane
continuation
warnings and incomplete reasons
```

Required behavior:

- exact, partial, alias, and typo filename navigation;
- semantic recall over authorized pending documents without treating them as confirmed knowledge;
- meaningful neighboring context for conversation hits;
- deterministic continuation;
- no conflation of search hit, verified evidence, and confirmed knowledge;
- fresh source authorization before reading and again before publication where required;
- no model-authored claim that a missing lane was searched successfully;
- no external search leakage of private archive names or queries;
- lower-level tools remain available for compatibility and expert calls.

The first release may be a read-only facade over existing engines. It does not need to replace all storage or retrieval implementation at once.

### 4. Durable Work Item semantics tied to evidence identity

The current `RecallConversation` canary is the right first slice. The durable contract should remain small.

A minimal Work Item may include:

```text
work_item_id
owner_id
kind
objective
constraints
status
created_at
updated_at
expires_at
revision
origin conversation and turn
active source scope
candidate SourceRef values
selected SourceRef and PassageRef values
accepted outcomes and receipt identities
resolved questions
unresolved questions
remaining steps
completion contract
completion state
publishable claims
last checkpoint
resume cursor
authorization or policy version that requires revalidation
```

Recommended statuses:

```text
OPEN
RUNNING
WAITING_FOR_USER
BLOCKED
PARTIAL
DONE
CANCELLED
STALE
```

The Work Item must distinguish:

```text
a capability call succeeded
the user task is complete
an answer was committed
an answer was delivered externally
```

These are separate events.

Required invariants:

- restart survival;
- ownership isolation;
- revision-CAS for concurrent updates;
- bounded expiry;
- explicit cancellation;
- authorization recheck on resume;
- accepted outcomes and receipts remain immutable historical facts;
- current source access and revision are re-attested before evidence is reused;
- no raw prompt or document body is copied into privacy-safe structural traces;
- deterministic completion can override contradictory model prose;
- a short follow-up updates only the dimensions it actually changes;
- a completed or expired Work Item is never silently resurrected.

Do not build a generic autonomous WorkGraph merely because the schema now has a durable Work Item. First prove one recall journey across messages, then extend it to documents using the stable retrieval identities above.

### 5. A common protocol for effectful actions

Typed read outcomes are only half of a cohesive assistant. A common effect envelope is needed before Friday broadly expands actions that change state.

Candidate fields:

```text
effect_id
work_item_id
requested action
authorization basis
idempotency key
attempt identity
status
side-effect receipt
reconciliation state
compensation state
evidence references
publishability
```

Required effect states should include at least:

```text
SUCCEEDED
PARTIAL
REFUSED
UNAVAILABLE
UNCERTAIN
COMPENSATED
```

`UNCERTAIN` is essential. A transport failure after a request was sent is not equivalent to a known failure. Friday must not blindly repeat an action or claim it did not occur.

The first common effect vertical should be narrow. Obsidian note mutation is the preferred candidate because its server-side operation, receipt, synchronization, conflict, and recovery contours are already relatively mature.

Target path:

```text
Work Item
    -> authorization
    -> typed mutation request
    -> idempotent execution
    -> accepted effect receipt
    -> synchronization observation
    -> remote edit or conflict
    -> incremental ingest
    -> reconciliation
    -> final completion and publication
```

Do not generalize this protocol across every connector before one real write-and-reconcile journey is proven.

### 6. Exact release artifact evidence

Friday production uses immutable wheel-only releases. The project must therefore prove the shipped artifact, not only an editable source checkout.

Introduce or expose one machine-readable evidence manifest for every release candidate.

Candidate fields:

```text
source commit
live commit
previous or fallback commit
package version
wheel SHA-256
database schema before and after
migration result
static gate result
non-UI gate result
UI gate result
clean-wheel install result
startup smoke result
production health observation
physical evidence state
backup and restore state
rollback artifact and rollback result
known skips with reason
```

The target release proof is:

```text
build the exact wheel
    -> install it into a clean environment
    -> migrate a production-like database copy
    -> start the service
    -> run the selected golden journeys and smoke probes
    -> activate immutably
    -> verify live source and wheel identity
    -> verify health and database integrity
    -> prove rollback to the previous sealed artifact
```

Resolve or explicitly document any apparent version-identity distinction between source package metadata and deployed release identity. The goal is not one particular numbering scheme. The goal is that source, wheel, activation, database schema, and rollback artifact can be reconciled without inference.

Do not claim UI, Docker, external-service, backup, or hardware evidence from a gate that did not execute those phases.

### 7. Physical and recovery evidence

Synthetic adapters are useful but cannot certify the last mile.

For the currently intended product boundary, one owner, one Android device, and one logical vault are sufficient. A multi-device or shared-vault expansion is not required before the first cohesive release.

The physical Android and Syncthing-Fork matrix must include:

```text
first onboarding
server-to-phone delivery
phone edit to incremental ingest
reconnect without duplicate application
real concurrent edit and conflict preservation
honest distinction between delivered, opened, and confirmed
restart during synchronization
```

Also perform a clean backup and restore drill:

```text
create a complete backup
    -> restore into a clean environment
    -> start Friday
    -> verify source identities and lifecycle state
    -> verify document and message discoverability
    -> verify citations
    -> resume or honestly invalidate an unfinished Work Item
```

Add bounded fault drills for the most important ambiguity classes:

- process termination during an action;
- expired credentials;
- provider timeout;
- duplicate inbound event;
- index lag behind the catalog;
- insufficient disk space;
- clock skew;
- uncertain outbound delivery.

A fault drill may be synthetic when the fault itself is synthetic, but hardware or external observations must remain labelled as such.

### 8. Minimal security and data-flow enforcement before broad expansion

The secure-workbench and assurance documents describe a larger target architecture. Do not attempt to implement the entire governance model at once.

Before broad external connectors or effectful MCP expansion, establish a minimum enforceable layer:

```text
SecurityLabel
ProjectionProfile
derived-from or taint tracking
ExternalDestinationPolicy
RetentionPolicy
AuditEvidence
```

Minimum rules:

- external transmission is default-deny unless policy explicitly permits it;
- a local source may have a local-model projection and a different external-model projection;
- derived answers retain the sensitive provenance needed for later policy checks;
- external tools do not receive private source content merely because the model selected them;
- deletion and retention semantics cover catalog projections, passages, embeddings, receipts, Work Items, and backups honestly;
- privacy-safe structural telemetry never becomes a hidden body store.

This is a product safety boundary, not an invitation to build an enterprise compliance platform.

### 9. Architecture hygiene and source-of-truth discipline

Several central modules are already very large. Do not begin a broad rewrite. Instead apply a ratchet:

```text
no new domain logic is added to an existing mega-module when a bounded service or adapter can own it
```

Use the new typed seams to extract small modules incrementally:

```text
work items
active frames
retrieval identity
archive search orchestration
completion gates
effect outcomes
publication
reconciliation
```

Avoid reverse dependencies from these modules back into model-specific runtime code.

The canonical status register must remain the current source of truth. Historical plans and handoffs should have an explicit historical banner or move to an archive location when appropriate. Add or extend automated documentation checks for:

- broken internal links;
- stale source and live identities;
- inconsistent package version declarations;
- schema declaration drift;
- active trackers that contradict the canonical status;
- missing evidence artifacts referenced as completed.

The currently missing `outer_sol/DOCUMENT_FILE_CONTOUR_WIP_AUDIT_2026-08-22.md` must remain an explicit unresolved source issue. Do not infer or claim its recommendations until the file is recovered or supplied.

## Ordered implementation packages

The architect should turn the following into small release packages. Exact package boundaries may change after code inspection, but the order and safety properties should remain.

### Package 0: finish the current P2 recall foundation and canary

Scope:

- schema-38 foundation with no behavior change;
- sealed schema-capable fallback release;
- exact current-conversation recall Work Item;
- closed temporal follow-up update;
- restart, expiry, cancellation, ownership, revision-CAS, receipt, and atomic rollback evidence.

Acceptance:

- no broad retrieval or WorkGraph abstraction is introduced;
- existing exact message-window outcome and citation contracts remain authoritative;
- no alternate model or source fallback appears on failure;
- production health and rollback remain clear;
- canonical and detailed trackers are updated after release.

### Package 1: product readiness and golden journey registry

Scope:

- one canonical journey table;
- explicit evidence classes;
- strict readiness states;
- links to executable tests, runbooks, and evidence manifests.

Acceptance:

- every `READY` claim points to concrete evidence;
- missing hardware or external evidence remains `UNVERIFIED`;
- component completion cannot silently imply journey completion;
- the registry is concise enough to remain current.

### Package 2: retrieval identity foundation

Scope:

- `SourceRef`;
- rebuildable `CatalogItem` projection;
- `PassageRef`;
- typed `TemporalFact`;
- `SearchCoverage`;
- backfill and compatibility state where required.

Acceptance:

- authorization remains owned by authoritative source stores;
- pending sources are discoverable only to authorized owners and remain non-confirmed;
- promotion does not destroy historical identity;
- aliases and filenames remain navigable;
- source revision and embedding incompatibility are explicit;
- no date role substitution;
- complete and partial search are distinguishable.

### Package 3: read-only `archive_search` facade

Scope:

- federated search plan over existing authorized lanes;
- unified candidates and coverage;
- document and message context;
- deterministic continuation;
- fresh authorization and evidence verification.

Acceptance:

- lower-level tools remain intact;
- no private query escapes to an external search provider;
- a failed or capped lane is visible in coverage;
- no confident absence is published from incomplete coverage;
- returned factual excerpts have valid locators and current source authority;
- difficult-query evaluation measures the shipped path, not a test-only searcher.

Recommended measurable release targets should include:

```text
100 percent of authorized live text-bearing files have a catalog and passages, or an explicit incomplete reason
candidate recall@50 at least 0.95 on the maintained difficult-query set
exact, partial, alias, and typo filename navigation passes
message-history hits include meaningful adjacent context
false-absence rate is measured and blocks release when coverage was incomplete
```

Threshold changes require measured evidence, not convenience.

### Package 4: durable recall Work Item across documents and messages

Scope:

- extend the proven recall Work Item from exact current-conversation windows to selected document and message evidence;
- retain stable source and passage references;
- preserve candidates, selection, remaining questions, and completion state;
- resume after restart and revalidate permissions.

Acceptance:

- selected evidence does not silently change after a follow-up;
- source mutation or permission change invalidates stale evidence honestly;
- short follow-ups update only changed dimensions;
- partial coverage remains partial after resume;
- completion is deterministic and independent of model enthusiasm;
- one end-to-end document recall journey and one conversation recall journey reach the declared readiness state.

### Package 5: one common effect envelope and Obsidian mutation vertical

Scope:

- typed effect states;
- idempotency;
- uncertain outcome reconciliation;
- accepted effect receipt;
- one Obsidian mutation path;
- synchronization and re-ingest observation.

Acceptance:

- retries cannot duplicate an accepted mutation;
- interrupted transport produces `UNCERTAIN` when appropriate;
- reconciliation can settle the final state without inventing success;
- a real conflict preserves both sides;
- the user-visible answer distinguishes action acceptance, synchronization observation, and physical-device confirmation.

### Package 6: release evidence manifest and clean artifact proof

Scope:

- machine-readable candidate manifest;
- clean-wheel install path;
- schema migration proof;
- startup and golden-journey smoke;
- immutable activation identity;
- rollback proof.

Acceptance:

- source, wheel, schema, activation, and fallback reconcile exactly;
- every skipped evidence phase has an explicit reason;
- no editable-install-only success is presented as artifact proof;
- the canonical status can be updated from the manifest without manual guesswork.

### Package 7: physical Android, backup, restore, and recovery certification

Scope:

- actual Android and Syncthing-Fork evidence;
- real edit and conflict matrix;
- complete backup and clean restore;
- selected fault drills.

Acceptance:

- physical observations are recorded separately from simulations;
- restored source and passage identities remain valid or are rebuilt deterministically;
- unfinished work resumes safely or becomes explicitly stale;
- rollback and restore do not duplicate effects;
- document and message golden journeys remain usable after restore.

### Package 8: evaluate the next expansion

Only after the packages above have evidence, decide whether the next need is:

- a generic WorkGraph;
- more effectful capability adapters;
- companion plugin work;
- additional devices or shared vaults;
- broader MCP connectors;
- model or inference-stack migration.

The decision must be based on remaining failed golden journeys, not architectural appetite.

## Explicit non-goals for the current convergence phase

Do not prioritize the following before the preceding gates are met:

- an unrestricted generic WorkGraph;
- broad autonomous multi-step execution;
- migration of every legacy tool to a new abstraction;
- companion plugin changes without operator approval;
- multi-user or shared-vault product expansion;
- large-scale mega-module rewrite;
- broad new MCP connector surface;
- a model-size upgrade presented as a fix for retrieval or work-state defects;
- Docker work in the current no-Docker release contour;
- claims of hardware, UI, external-service, or restore readiness without direct evidence.

## Cross-cutting invariants for every package

Every release package must preserve these invariants:

```text
small and independently releasable
source and live identity recorded
rollback-safe fallback preserved
no silent authorization widening
no user body in privacy-safe structural telemetry
no weakening of an existing check merely to make a gate green
no model fallback after a deterministic lane has failed closed unless the contract explicitly permits it
no duplicate effect on retry
no false claim of complete search
no physical or external claim from synthetic evidence
canonical status updated after release
```

Production user content and the live database must not be used as a casual test playground. Any real-data audit must follow the existing approved read-only, offline-copy, secret-free boundary and must report exactly what was and was not inspected.

## Questions the architect must answer in the implementation plan

The architecture review preceding Packages 2 through 5 should explicitly answer:

1. Is the schema-38 Work Item foundation generic enough to reference future `SourceRef` and `PassageRef` values without schema churn, while still remaining small?
2. Which current authoritative identities map to the first `SourceRef` implementation?
3. How is identity preserved across raw, pending, promoted, moved, renamed, and deleted states?
4. Which lifecycle transitions require catalog rebuild, passage rebuild, or embedding backfill?
5. How are date roles represented and queried without substitution?
6. How is search coverage calculated per lane, and what makes it `complete`?
7. How does a Work Item pin candidates and a selected source without copying private evidence into structural state?
8. Which events require fresh authorization or source revision re-attestation?
9. How is an effect classified when transport fails after dispatch?
10. Which exact artifact identities are required to reproduce, activate, and roll back a release?
11. What direct evidence changes each golden journey from `UNVERIFIED` or `DEGRADED` to `READY`?

Unresolved answers must appear as explicit risks or deferred decisions. They must not disappear into implementation prose.

## Required architect deliverables

For each package, produce:

- a current-code audit of the affected path;
- a concise architecture decision or confirmation;
- exact contracts and ownership boundaries;
- schema and migration plan where applicable;
- rollback and compatibility plan;
- deterministic and adversarial acceptance tests;
- a narrow live or physical evidence plan where relevant;
- one or more small commits;
- updated canonical and detailed status documents;
- a final evidence summary containing source, live, fallback, schema, gates, known skips, and next item.

Do not create a second competing roadmap. The canonical status register should remain short and current; this brief and the related architecture documents provide the deeper rationale.

## Definition of a cohesive first product release

Friday may be described as a cohesive first product when all of the following are true:

1. The canonical golden-journey registry exists and contains no unsupported `READY` claim.
2. Document and conversation recall use stable source and passage identity.
3. Search coverage can represent complete, partial, stale, unavailable, permission-filtered, capped, and incompatible lanes.
4. Friday cannot confidently claim archive absence from incomplete coverage.
5. One durable recall Work Item survives restart, follow-up, expiry, cancellation, permission change, and source revision.
6. One effectful Obsidian journey is idempotent, receipt-backed, and reconcilable after uncertainty.
7. The exact wheel artifact, schema migration, live activation, and rollback are machine-reconciled.
8. One actual Android device has passed the intended synchronization and conflict path.
9. A clean backup restore preserves or deterministically rebuilds discoverability, citations, and safe work-state handling.
10. The system remains honest under a missing provider, stale index, interrupted action, or uncertain delivery.

This boundary does not require every future capability, a generic WorkGraph, a companion plugin, multiple devices, or a larger model. It requires a small number of complete, dependable journeys.

## Final direction

Friday has already crossed the difficult conceptual threshold: it is building typed outcomes, receipts, traces, and completion semantics before granting broad autonomy.

The next task is not to replace that direction. It is to close the loop around it.

Use the current `RecallConversation` P2 slice as the first durable end-to-end reference implementation. Then give Work Items stable retrieval identities to hold, give search an honest coverage contract, give effects an uncertainty-aware receipt protocol, and prove the resulting journeys through the exact shipped artifact, restart, restore, and the actual device.

The project should advance when a user journey becomes demonstrably trustworthy, not merely when another organ gains a new function.
