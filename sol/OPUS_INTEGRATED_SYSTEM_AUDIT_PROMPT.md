# Prompt for Opus: Audit and Complete Friday as a Local-First Ontology and Agentic Operations Platform

## Mission

You are Opus, acting as Friday's principal architect, security reviewer, product owner, and implementation lead. Audit the current `origin/main` of Friday against the target system defined below, then immediately implement every confirmed, feasible gap in priority order. Do not stop after writing an assessment or backlog. Continue through implementation, tests, documentation, full repository gates, commits, and verified pushes to `main` until all actionable P0 and P1 gaps are closed and every remaining item has an evidence-backed disposition.

The target is not a clone of Jarvis, Palantir, or any proprietary product. It is a coherent local-first system that combines:

- Friday's reviewed knowledge lifecycle, provenance, graph, hybrid retrieval, and tenant-safe Knowledge OS;
- Jarvis's durable agent execution, missions, typed tools, approvals, verification, recovery, document intelligence, multimodal interaction, and operator-grade reliability;
- Palantir-like ontology-centered operational modeling, object/link/action semantics, lineage, object views, monitored workflows, and governed writeback.

Treat this file as a product and engineering acceptance specification, not as an invitation to add fashionable infrastructure. Preserve the current architecture wherever it already satisfies the contract. Extend existing seams; do not build parallel registries, duplicate policy engines, or second execution runtimes.

## Operating rules

1. Fetch `origin/main`, read every current repository instruction before acting, and work from a clean, isolated worktree.
2. Code, schema, migrations, tests, and runtime behavior are the source of truth. Documentation is a claim that must be verified.
3. Do not mark a capability complete because a class, route, flag, README paragraph, mock, or isolated unit test exists. Prove the end-to-end user journey and its failure paths.
4. Trace existing implementations before proposing replacements. Prefer completing or connecting an existing subsystem over adding a new one.
5. Do not silently change retrieval, extraction, classification, authorization, approval, or canonical knowledge behavior. Before such a change, record the evidence, refusal case, predeclared acceptance metric, cost, and risk in the appropriate proposal/task record.
6. Measure before changing ranking, graph expansion, weights, thresholds, extraction semantics, or automatic entity creation. Existing negative experiments and completed handoffs are binding evidence unless a new assignment explicitly reopens them.
7. Preserve local-first operation and private-data boundaries. Never add proprietary user content, production queries, document text, secrets, credentials, stable identifiers, or recognizable private-domain vocabulary to the repository, tests, logs, prompts, or handoff files.
8. A selector identifies a target; it never grants authority. All tenant, user, project, object, file, and conversation access must be reauthorized server-side at the point of use.
9. Models are untrusted reasoning components. They do not create canonical facts, grant permissions, approve side effects, declare execution success, or bypass review.
10. Use one `ExecutionKernel`, one capability registry/gate, one typed tool contract, one tenant context, and one audit path. Do not create a shadow execution system.
11. Mutating operations require exact intent, authorization, bounded scope, idempotency, durable state, independent postcondition verification, and an explicit outcome. Ambiguous outcomes become terminal `uncertain`; they are never blindly replayed.
12. Keep Telegram as the first-class maximum-functionality interface. HTTP, admin UI, and CLI may add operator ergonomics but must not become the only way to use a core end-user capability.
13. Stage only owned files. Preserve unrelated changes. Do not force-push. Do not push a red gate to `main`.
14. Finish each independently releasable wave with the complete repository gate required by `sol/SOL.md`, including the required mutation proof for new tests, then commit and push directly to `main` according to project rules.
15. If a task genuinely requires private live evidence, an external operator, or a material product decision, fail closed. Publish the smallest deidentified handoff contract that would unblock it; do not invent evidence or substitute synthetic data for a live acceptance requirement.

## Target product definition

Friday should be a local-first, multi-user operational knowledge and action platform. A user must be able to turn heterogeneous private material into reviewed, typed, connected knowledge; ask evidence-grounded questions; understand people, projects, systems, events, decisions, risks, and changes over time; plan and execute bounded work through governed tools; and operate all of this with strong tenant isolation, provenance, observability, recovery, and human control.

The system is complete only when the following capability domains work as coherent end-to-end flows.

### 1. Reviewed knowledge foundation

- Every inbound item first becomes an immutable Raw Object with tenant ownership, source metadata, integrity hash, ingestion time, parser version, and lineage.
- Parsing, OCR, transcription, vision, extraction, and model outputs create reviewable Inbox candidates, never canonical truth.
- Explicit review promotes, edits, rejects, merges, or supersedes candidates into versioned Knowledge Objects.
- Every canonical assertion can be traced back through transformations to exact source evidence and the original Raw Object.
- The lifecycle supports correction, supersession, archival, retention, export, and deletion without destroying required audit evidence.
- Duplicate ingestion is idempotent. Near-duplicate detection suggests review actions but does not silently merge canonical knowledge.
- Bulk ingestion remains bounded and reviewable. Backpressure, quotas, parser budgets, and partial failures are visible.
- Unsupported, suspicious, encrypted, malformed, oversized, or adversarial content fails safely and remains quarantined with a useful operator explanation.

### 2. Versioned operational ontology

- The ontology represents at least Documents, Knowledge Objects, People, Organizations, Projects, Assets or Systems, Events, Decisions, Tasks, Locations, Policies, Risks, and Metrics where evidence supports those types.
- Object types, properties, value types, link types, constraints, statuses, and lifecycle rules are explicit, typed, versioned, and migration-safe.
- Relations carry provenance, confidence, temporal validity, creator, review state, and tenant scope rather than existing as unexplained edges.
- Identity resolution supports candidate matches, explicit merge and unmerge, aliases, conflict review, and durable provenance.
- Temporal queries distinguish event time, source time, ingestion time, validity intervals, and correction time.
- Derived properties and aggregates identify their source objects, calculation version, and freshness. A derived value is never presented as a sourced fact.
- Ontology changes have schema compatibility checks, migration plans, impact visibility, and rollback or repair procedures.
- Canonical object edits use controlled actions with authorization, validation, audit, and review where required.
- Object-centered views can show the current state, linked evidence, history, decisions, tasks, risks, metrics, responsible people, and allowed actions.
- Scenario or what-if state is isolated from canonical state, visibly labeled, reversible, and incapable of silently becoming truth.
- Do not automatically create Risk or Metric objects, or any other weakly measured semantic class, merely to satisfy this catalog. Retain a reviewed or derived view until extraction quality meets a predeclared live acceptance threshold.

### 3. Ingestion and document intelligence

- Support the repository's declared text, office, PDF, image, audio, archive, web, and structured-data formats through safe, bounded parsers.
- Preserve document structure, page or section anchors, tables, attachments, metadata, and chunk-to-source coordinates needed for precise citation.
- OCR, transcription, and vision are optional capabilities with explicit availability, confidence, latency, and fallback behavior.
- Users can inspect, read, summarize, compare, review, and extract structured information with citations.
- Document edits are planned, reviewable, copy-on-write by default, format-aware, and verified after rendering or reopening. Originals remain recoverable.
- Comparisons and redlines distinguish source changes from model commentary.
- Export preserves tenant scope, provenance, object identity, and redaction policy.
- Email, calendar, bookmarks, browser capture, connectors, and watched folders may feed the same reviewed ingestion contract; none may bypass it.
- Downloads and remote content pass SSRF, DNS-rebinding, content-type, size, archive-expansion, malware/quarantine, and prompt-injection boundaries.

### 4. Evidence-grounded retrieval and analysis

- Hybrid retrieval may combine lexical, full-text, embedding, field, graph, lifecycle, quality, feedback, passage, and reranking signals through explicit, inspectable stages.
- Query classification selects retrieval behavior only when measured. Ordinary queries remain on safe defaults; graph expansion and reranking are mode-dependent and observable.
- Every answer claim that depends on stored knowledge cites stable evidence anchors such as `[K#]`, and those anchors open the supporting object and raw source.
- The answer path maintains an evidence ledger: considered sources, selected sources, discarded sources, channel contributions, transformations, and final claim support.
- The system abstains or reports uncertainty when evidence is absent, conflicting, stale, low-confidence, unauthorized, or outside the available corpus.
- Retrieval never leaks existence, counts, snippets, ranks, embeddings, identifiers, timing differences, or error details across tenants.
- Users can ask relational, temporal, comparative, contradiction, trend, project, person, decision, and change-over-time questions.
- Graph answers identify the path and evidence for each relation. Inferred paths are labeled and cannot be silently promoted to canonical links.
- Contradictions, stale facts, duplicate entities, and unresolved identity matches enter review workflows instead of being averaged away.
- Feedback is tenant-scoped, attributable, reversible, abuse-resistant, and evaluated before it changes ranking.
- Evaluation traces are deidentified, contract-validated, reproducible, and fail closed. Search behavior changes require frozen gold sets and predeclared metrics.
- Web research is treated as untrusted supplementary evidence. It carries source identity, retrieval time, quotation boundaries, and claim-level citations; it becomes canonical knowledge only through review.

### 5. Durable agent and mission execution

- The conversational agent separates dialogue, knowledge work, research, planning, and execution modes with explicit transitions.
- Complex work is represented as a durable mission DAG with typed steps, dependencies, budgets, deadlines, retries, cancellation, replanning, assertions, and resumable state.
- Observation and analysis tools are distinct from mutating tools. The model may propose actions; the service authorizes and executes them.
- Every tool has a typed `ToolSpec`, exact owner, capability requirements, input and output schemas, risk class, budget semantics, timeout, idempotency policy, audit fields, and verification contract.
- Process execution is argv-only and allowlisted. Filesystem, network, registry, browser, document, messaging, and host-bridge effects are deny by default and scoped to declared resources.
- Capability snapshots and sealed material scopes bind authorization to tenant, actor, resource set, tool, normalized arguments, policy epoch, and relevant environment digest.
- Human approval is durable and bound to the exact normalized action payload, actor, tenant, target, risk, policy version, expiry, and one-shot claim. Any material change invalidates it.
- Approval claim is atomic. Execution reauthorizes immediately before the side effect and independently verifies postconditions afterward.
- A successful tool call is not proof of task success. Readiness claims and mission completion require independently checked assertions.
- Side-effecting steps have checkpoints and rollback or compensation where safe. Irreversible operations declare that fact before approval.
- Crash recovery distinguishes not-started, executing, succeeded, failed, compensated, cancelled, and uncertain. Uncertain side effects require reconciliation, not automatic replay.
- Retry, recursion, token, time, network, storage, process, and monetary budgets are enforced below the model.
- A result can receive at most a bounded repair pass after failed verification; the system must not loop until it can claim success.
- Agent outputs and generated artifacts are Inbox candidates until reviewed when they purport to alter canonical knowledge.

### 6. Ontology-centered operational workflows

- Users can navigate from an object to linked people, projects, evidence, events, decisions, tasks, risks, metrics, and permitted actions.
- Work queues and cases represent reviewable operational state with assignment, priority, SLA, status, comments, evidence, and a complete change log.
- Actions can create or update operational objects only through typed, authorized, validated writeback with preview, approval where required, and postcondition checks.
- Decisions record who decided, what evidence was available, alternatives considered, expected outcome, policy basis, and observed result.
- Tasks and missions remain linked to the objects, evidence, approvals, and decisions that caused them.
- Monitors and subscriptions evaluate explicit conditions against authorized object sets and produce deduplicated, rate-limited notifications or review items.
- Operational dashboards expose current state, trends, exceptions, freshness, provenance, and data quality without implying certainty that the data does not support.
- Lineage and impact analysis answer where a value came from, which transformations and versions produced it, and which objects, views, monitors, or decisions would be affected by a change.
- Scenario analysis is reproducible and isolated. Assumptions, input versions, model versions, uncertainty, and comparison with canonical state are visible.
- Supervisory or cross-tenant views exist only for explicitly authorized roles and still enforce purpose, scope, logging, and sealed material access.

### 7. Human interfaces and product experience

- Telegram supports ingestion, review, search, evidence opening, missions, approval, cancellation, status, retry or reconciliation guidance, reminders, exports, and safe document delivery.
- HTTP APIs, CLI, and admin UI use the same service contracts and authorization checks; no interface owns a privileged shortcut.
- Conversation history supports search, rename, archive, export, delete, regeneration, durable focus, and correct attachment or source follow-ups.
- Voice, image, and document interactions preserve the same tenant, provenance, review, and safety rules as text.
- Users can see what the system knows, why it answered, what it plans to do, what it did, what remains uncertain, and how to correct it.
- Approval prompts show the exact action, target, scope, important arguments, risk, reversibility, expected evidence of success, and expiry in human language.
- Errors are actionable but do not reveal secrets, internal policy, cross-tenant state, or unsafe retry instructions.
- Long-running work provides durable, rate-limited progress and a final outcome without notification spam.
- User preferences and persona improve interaction but cannot weaken authorization, evidence, or review gates.
- Accessibility, localization, consistent timestamps, and stable object references are treated as product requirements rather than UI polish.

### 8. Multi-user security, governance, and privacy

- Authentication is explicit for every ingress. Telegram bridge messages are signed, replay-resistant, idempotent, and tied to the resolved server-side principal.
- Authorization is default deny and enforced at repository, query, object, link, file, raw source, passage, embedding, cache, job, event, approval, tool, and export boundaries.
- Tenant scope is mandatory in persistence keys, unique constraints, caches, queues, vector operations, graph traversal, logs, and background work.
- Owner or admin cross-user material access rechecks exact target identity and purpose at delivery time. Account names, usernames, filenames, or conversation text never grant access.
- Policy and capability decisions are server-owned, versioned, audited, and never delegated to prompt instructions.
- Prompt injection from documents, web pages, tool output, OCR, metadata, filenames, or quoted messages is treated as untrusted data, not authority.
- Browser and network tools enforce public-only destinations unless an explicit capability authorizes a narrower private target. Redirects and resolved addresses are rechecked.
- Secrets are loaded from approved stores, never returned to models, redacted from logs, excluded from artifacts, and rotated without code changes.
- Sensitive audit events are append-only or tamper-evident and include actor, tenant, request, normalized action, decision, policy version, outcome, and correlation identifiers.
- Retention, legal hold where applicable, export, deletion, backup, restore, and disaster-recovery semantics are documented and tested.
- Security tests include confused deputy, indirect prompt injection, cross-tenant inference, IDOR, stale approval, payload substitution, replay, duplicate delivery, SSRF, DNS rebinding, archive bombs, malicious documents, command injection, and crash-at-side-effect-boundary cases.

### 9. Reliability, observability, and operations

- Every durable workflow has explicit state, lease or claim semantics, idempotency, retry limits, dead-letter or reconciliation behavior, and operator visibility.
- Health and readiness distinguish API availability, database health, migration state, queue lag, worker leases, model endpoints, embeddings, reranker, OCR, browser, storage, and external connectors.
- Correlation identifiers link ingress, conversation turn, retrieval trace, mission, approval, tool execution, object mutation, audit event, and user delivery.
- Metrics cover latency, availability, error rate, queue lag, duplicate suppression, retrieval quality, citation validity, approval outcomes, uncertain executions, tenant-isolation denials, delivery outcomes, and resource use.
- Logs are structured, bounded, redacted, tenant-safe, and sufficient for incident reconstruction without containing private document content.
- Backups are encrypted where appropriate, versioned, restorable, and tested through an actual restore drill including schema and object integrity checks.
- Schema migrations are atomic or resumable, forward-tested, downgrade-aware where practical, and blocked when runtime and schema versions are incompatible.
- Model, embedding, reranker, OCR, browser, and connector outages degrade explicitly. Core reviewed knowledge remains accessible when optional AI services are unavailable.
- GPU, CPU, RAM, disk, PID, network, time, and concurrency limits are explicit. Heavy jobs cannot starve ingestion, review, or interactive requests.
- Startup, shutdown, restart, deployment, and worker replacement preserve durable work and never manufacture success.
- Incident runbooks cover data corruption, queue poisoning, stuck approvals, uncertain side effects, tenant-leak suspicion, model endpoint failure, and failed restore.

### 10. Extensibility and maintainability

- Organs and JOP extensions expose capabilities, workers, routes, migrations, and configuration through narrow documented interfaces.
- Extensions cannot bypass `ExecutionKernel`, tenant context, capability gates, audit, review, budgets, or safe parsing.
- Domain logic is testable without a live model. Model adapters are replaceable and capability-detected.
- Public contracts are typed and versioned. Breaking changes have migrations and compatibility tests.
- Configuration has explicit precedence, safe defaults, validation, diagnostics, and secret separation.
- There is no hidden production behavior available only through undocumented environment variables or ad hoc operator steps.
- Architectural boundaries are enforced by tests or static checks where feasible, not only by convention.

### 11. Continuous quality and evidence

- Maintain deidentified golden sets for ingestion, extraction, entity resolution, retrieval, graph reasoning, citations, document edits, missions, approvals, tenant isolation, Telegram delivery, and recovery.
- Declare acceptance criteria before examining candidate results. Record latency and resource cost alongside quality.
- Separate synthetic correctness tests from live configured-stack acceptance. Do not claim production readiness from mocks alone.
- Runtime- or model-facing changes receive a safe live acceptance through the configured local stack when private evidence and authorization are available.
- Every new critical test has a demonstrated mutation or equivalent proof that it can detect the intended regression.
- Negative results remain first-class engineering evidence. Do not rerun or tune around them without a new hypothesis and predeclared contract.
- Release notes distinguish implemented behavior, measured behavior, experimental behavior, disabled behavior, and planned work.

## Mandatory end-to-end journeys

Audit these journeys through real service boundaries. Add missing acceptance tests and implement the smallest durable fixes that make each journey true. A journey is not complete if only its happy path works.

1. A tenant ingests a supported document, receives an immutable Raw Object and a reviewable candidate, approves it, retrieves it later, opens `[K#]`, and reaches the exact source span.
2. The same document is delivered twice through the same and different ingress paths; the system behaves idempotently without losing lineage or silently merging ambiguous content.
3. A malformed, oversized, encrypted, or adversarial file is quarantined without parser escape, resource exhaustion, private-data logging, or a false success message.
4. A user corrects an extracted entity or relation; the correction is versioned, linked to evidence, reflected in retrieval, and reversible without editing the Raw Object.
5. Two possible identities are proposed, reviewed, merged, and later unmerged while all historical links and provenance remain interpretable.
6. A relational question uses graph expansion only for the measured query class and returns evidence-backed relation paths; an ordinary query does not pay the graph-expansion behavior or cost.
7. A temporal question distinguishes when an event happened, when a source reported it, and when Friday learned or corrected it.
8. Conflicting sources produce an explicit contradiction or review item rather than an invented consensus.
9. A project view connects its reviewed people, documents, events, decisions, tasks, supported risks, supported metrics, recent changes, and permitted actions.
10. A research answer combines stored and web evidence, labels external evidence, cites each material claim, rejects injected instructions, and does not silently promote web content into canonical knowledge.
11. A document comparison or edit produces a reviewable plan and copy, preserves the original, verifies the resulting artifact, and exposes a redline or failure evidence.
12. A multi-step mission survives a process restart, resumes from durable state, respects budgets, and proves its completion assertions independently.
13. A dangerous action requests approval with the exact normalized payload; approval is one-shot, atomically claimed, invalidated by payload or policy changes, and audited.
14. A crash occurs exactly around a side effect; the mission enters `uncertain`, reconciles through observation, and never blindly repeats the effect.
15. A tool reports success while the postcondition is false; Friday refuses to mark the step or mission complete and performs at most the bounded repair policy.
16. An unauthorized user names another account, project, file, conversation, object ID, or tenant. Resolution may identify a target, but every read and delivery remains denied without server-side authority.
17. An authorized owner or administrator accesses a sealed cross-user material scope; the exact target and purpose are rechecked, minimized, audited, and prevented from contaminating ordinary tenant memory.
18. A model, embedding service, reranker, OCR service, or browser is unavailable. Friday degrades visibly and safely while unaffected local knowledge operations continue.
19. Telegram receives duplicate updates, delayed work, and a delivery failure. Processing and delivery are separately durable, duplicates are suppressed, and operators can distinguish completed work from confirmed delivery.
20. A reminder or monitor fires once under concurrency, is tenant-scoped, produces a deduplicated notification, and records its condition and source state.
21. A backup is restored into a clean environment and passes schema, tenant, raw-object hash, provenance, object-link, approval, and workflow integrity checks.
22. A schema or ontology change identifies impacted queries, views, monitors, derived values, tools, and migrations before activation.
23. A scenario changes assumptions without mutating canonical objects, compares outcomes with the canonical baseline, and can be discarded without residue.
24. A user exports and deletes allowed personal data; the operation respects retention and audit rules and leaves no unauthorized cache, vector, graph, queue, or artifact residue.
25. An extension is installed or enabled and cannot access data, spawn work, register tools, or mutate objects outside its declared capabilities and tenant context.

## Audit method

### Step 1: Establish the evidence baseline

- Record the exact `origin/main` commit, Python and Node versions, configured optional services, schema version, and gate commands.
- Read `README.md`, `TASKS.md`, `sol/SOL.md`, `sol/TASKS.md`, `sol/PROPOSALS.md`, architecture, security, organ, migration, and handoff documentation.
- Inspect repository structure with targeted search. Trace the actual routes, services, repositories, schemas, workers, Telegram handlers, agent loop, execution kernel, capability registry, tools, missions, approvals, retrieval, graph, ingestion, document handling, audit, backup, and UI or CLI seams.
- Review recent commits and completed experiment records so the audit does not reopen finished or rejected work accidentally.
- Run the baseline full gate before behavior changes. Distinguish pre-existing failures from your work and do not hide or normalize them.

### Step 2: Build a capability evidence matrix

Create a durable audit artifact in the repository with one row for every requirement and journey above. Each row must contain:

| Field | Required content |
|---|---|
| Capability ID | Stable domain and sequence identifier |
| User outcome | Concrete user-visible result |
| Status | `complete`, `present_disconnected`, `partial`, `unsafe`, `missing`, `intentional_absence`, or `blocked` |
| Code evidence | Exact files, symbols, schema, routes, and tests |
| Runtime evidence | Acceptance command, trace, or reason it cannot safely run |
| Failure path | What happens on denial, timeout, crash, duplicate, stale state, or ambiguity |
| Tenant and policy boundary | Where scope and authorization are enforced |
| Provenance boundary | How inputs, transformations, claims, and mutations are traced |
| Gap | Smallest missing contract, not a vague feature label |
| Risk | Security, correctness, privacy, reliability, product, and migration risk |
| Acceptance metric | Predeclared, falsifiable completion criterion |
| Cost | Expected latency, storage, compute, operations, and migration cost |
| Disposition | Implemented commit, proposal, deidentified handoff, intentional rejection, or exact blocker |

Use `complete` only when code and end-to-end evidence agree. Use `present_disconnected` when components exist but no supported user path reaches them. Use `unsafe` when the capability exists by bypassing a non-negotiable boundary. A disabled flag is not completion unless disabled behavior is explicitly the accepted product state.

### Step 3: Trace the system by boundary, not by directory

For every end-to-end journey, trace:

`ingress -> identity -> tenant context -> persistence -> parsing or retrieval -> model context -> tool proposal -> capability decision -> approval -> execution -> verification -> canonical mutation or review candidate -> audit -> user delivery`

At each arrow, identify schemas, trust changes, idempotency keys, timeouts, budgets, authorization, error handling, retries, audit evidence, and privacy exposure. Search specifically for:

- unused implementations and feature flags;
- routes that bypass services or repositories;
- repositories missing tenant predicates;
- caches, vectors, graph traversals, jobs, or logs without tenant keys;
- policy decisions delegated to prompts or model text;
- approval reuse or argument drift;
- success inferred from tool output;
- automatic retries after ambiguous side effects;
- canonical writes from extraction or agent output;
- missing provenance across parsing, chunks, links, derived values, and citations;
- workers without leases, idempotency, bounded retries, or reconciliation;
- UI or Telegram messages that overstate completion or delivery;
- mocks presented as runtime readiness;
- documentation that describes code paths that no longer exist.

### Step 4: Threat-model the gaps

For every mutating or cross-boundary feature, write at least one refusal scenario showing when the system must not proceed. Include malicious and accidental cases. Prioritize evidence for:

- cross-tenant inference and confused deputy behavior;
- prompt injection through every untrusted content channel;
- stale policy, stale approval, payload substitution, replay, and duplicate claim;
- SSRF, redirects, DNS rebinding, local service access, and download expansion;
- parser or renderer escape and resource exhaustion;
- forged delivery success, duplicate notifications, and crash-boundary ambiguity;
- incorrect identity merge, unsupported derived facts, and lineage loss;
- optional-service outage and partial migration behavior.

### Step 5: Rank work into executable waves

- **P0 — safety and integrity:** tenant leak, authorization bypass, unreviewed canonical write, approval weakness, side-effect replay, false completion, provenance break, unsafe parser or network boundary, corrupt migration or restore.
- **P1 — broken core journey:** a declared user capability is absent, disconnected, unreliable, or unavailable in Telegram; durable work cannot resume; citations do not open evidence; delivery state is false; optional-service failure breaks core knowledge access.
- **P2 — operational intelligence:** missing ontology action, object view, case or review workflow, monitor, lineage impact, temporal or contradiction analysis, decision record, scenario isolation, or measured retrieval improvement.
- **P3 — usability, scale, and efficiency:** operator ergonomics, performance, resource cost, broader format support, richer visualizations, and low-risk polish.

Do not use priority to excuse a confirmed P0 or P1 gap. A large gap must be decomposed into independently safe, releasable slices with a clear final contract.

## Implementation mandate

1. Finish the complete audit before making architectural bets, but immediately fix an independently proven P0 if leaving it in place creates active risk.
2. For every confirmed P0 and P1 gap that does not require private evidence or a user decision, implement it now. Do not merely add it to `TASKS.md`.
3. Implement P2 gaps when the evidence and acceptance metric are available and the change fits existing architecture. Otherwise publish a concrete proposal or deidentified handoff that makes the next action executable.
4. Do not build P3 work while actionable P0 or P1 work remains.
5. Start with the smallest root-contract fix that closes the user journey. Avoid per-user workarounds, prompt-only policies, special-case identifiers, and UI-only masks.
6. Reuse the existing kernel, capability, approval, review, mission, retrieval, graph, organ, worker, audit, and delivery abstractions. If an abstraction is incomplete, repair it at its authority boundary.
7. Add schema migrations only with compatibility, backfill, restart, backup, and restore evidence.
8. Add focused tests before or with the fix: positive path, denial, ambiguity, duplicate, timeout, crash or retry, tenant isolation, and provenance as applicable.
9. Demonstrate the mandatory mutation for each new critical regression test.
10. Run focused tests during iteration, then the exact full gate from `sol/SOL.md` before every push.
11. For runtime- or model-facing behavior, run a safe live acceptance against the configured local stack when allowed. Record only deidentified outcomes and metrics.
12. Keep commits small enough to review and revert, but complete enough to leave `main` green and useful. Rebase safely, stage only owned files, push verified commits to `main`, and verify the remote commit.
13. Continue wave by wave without waiting for encouragement. Stop only for a new material blocker, required user choice, privacy boundary, unavailable live handoff, or external authority.

## Quantitative acceptance floor

Do not invent one universal score for the entire system. Declare domain-specific thresholds before candidate implementation. At minimum, every completed capability must show:

- zero cross-tenant disclosures in adversarial tests;
- zero unauthorized side effects;
- zero unreviewed canonical fact creation from model or extraction output;
- zero blind replay of an uncertain side effect;
- zero citations that resolve to a different tenant or unsupported source;
- deterministic idempotency for duplicate ingress and duplicate action claims;
- bounded resource behavior under the repository's documented limits;
- recovery evidence for restart-sensitive workflows;
- a measurable user outcome, not only code coverage;
- full repository gate success and required mutation evidence.

For quality optimizations, require a frozen baseline, deidentified gold set, failure budget, no-regression constraints, latency and resource cost, and a predeclared minimum net gain. A change that improves an aggregate while causing an unacceptable safety or baseline regression fails.

## Explicit anti-goals

- Do not rewrite Friday around a new database, graph engine, vector store, queue, UI framework, agent framework, or cloud platform without a measured blocker that existing architecture cannot solve.
- Do not create a second tool registry, capability system, policy engine, tenant context, audit stream, review queue, or mission runtime.
- Do not make prompts the enforcement layer for permissions, approvals, provenance, data retention, or side effects.
- Do not give the model arbitrary shell, filesystem, browser, network, database, or messaging power.
- Do not let web content, connector data, agent summaries, inferred relations, or extraction output become canonical truth without the required review path.
- Do not infer access from a username, filename, object name, project name, chat text, model decision, or successful lookup.
- Do not optimize ranking or ontology extraction by repeatedly tuning on the acceptance set.
- Do not report a feature as complete when it is disabled, unreachable, undocumented, mock-only, single-tenant by accident, or missing recovery behavior.
- Do not replace exact failure evidence with generic "needs more testing" language.
- Do not copy proprietary implementations or branding. Borrow only general product principles: ontology-centered modeling, lineage, governed actions, operational views, monitored workflows, and decision accountability.

## Required deliverables

Produce and maintain all of the following as part of the work:

1. A versioned integrated audit document containing the capability matrix, end-to-end journey evidence, threat findings, current commit, and gate baseline.
2. A prioritized execution plan mapped to stable capability IDs, owners, dependencies, acceptance criteria, and status.
3. Evidence-backed proposals for every behavior-changing search, extraction, rights, schema, ontology, or execution decision that is not already authorized by this specification and current project rules.
4. Implemented code, migrations, tests, and documentation for every actionable P0 and P1 gap and every feasible accepted P2 gap.
5. Deidentified, fail-closed handoff contracts for the few items that genuinely require private live inputs or another operator.
6. A completion ledger mapping each gap to commits, focused tests, mutation proof, live acceptance where applicable, full-gate result, and verified push.
7. A concise final report containing:
   - target commit and pushed commits;
   - capabilities completed, repaired, connected, rejected, or blocked;
   - exact gates and counts;
   - live acceptance results without private content;
   - remaining blockers, the minimum unblocker for each, and why they could not be completed safely.

## Definition of done

This assignment is complete only when:

- every target requirement and mandatory journey has an evidence-backed matrix disposition;
- all feasible P0 and P1 gaps are implemented and verified, not merely scheduled;
- remaining P2 and P3 work is either completed or justified by evidence, cost, risk, a predeclared experiment, or an exact external blocker;
- no new parallel authority system, privacy regression, tenant leak, unreviewed canonical write, blind side-effect replay, or false-completion path was introduced;
- repository documentation, task state, migrations, configuration, and runtime behavior agree;
- focused tests, mutation requirements, safe live acceptance where applicable, and the full repository gate are green;
- only owned files were committed, commits were safely integrated, and verified changes reached `origin/main`;
- the final report is short, factual, and links every claim to evidence.

Do not answer with a vision deck. Audit the real system, prove the gaps, and complete the work.

## Conceptual references

Use these only to clarify general product principles; Friday's local-first, tenant-safe, reviewed architecture remains authoritative:

- Friday's current repository architecture, security, organ, task, proposal, and Sol law documents.
- Jarvis's current repository architecture and acceptance records, especially durable typed execution, missions, approval binding, independent verification, recovery, document intelligence, and multi-user material isolation.
- Palantir's public descriptions of Ontology object/link/action concepts and data lineage:
  - <https://www.palantir.com/docs/foundry/ontology/overview/>
  - <https://www.palantir.com/docs/foundry/data-lineage/overview/>
