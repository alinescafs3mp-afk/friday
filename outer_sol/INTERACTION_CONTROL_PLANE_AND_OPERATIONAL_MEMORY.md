# Friday Interaction Control Plane and Operational Memory Architecture

> Document ID: FRIDAY-ICP-001  
> Status: active architecture reference; live state and order are owned by
> [`PROJECT_BACKLOG.md`](PROJECT_BACKLOG.md)
> Repository snapshot: `main`, Friday `0.205.0`, 20 August 2026  
> Scope: cross-capability coordination, persistent work state, continuation resolution, workflow composition, capability outcomes, completion semantics, failure recovery, V12 integration, and episode-level evaluation.  
> Related documents: [`PROJECT_BACKLOG.md`](PROJECT_BACKLOG.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md), and [`SYSTEM_ASSURANCE_AND_RECORDS_GOVERNANCE.md`](SYSTEM_ASSURANCE_AND_RECORDS_GOVERNANCE.md).

## Executive conclusion

Friday already contains many individually valuable capabilities:

- ordinary conversation;
- document ingestion and review;
- document and message retrieval;
- web search and research;
- entity and graph lookup;
- file generation;
- reminders and other tools;
- durable missions;
- permissions, provenance, verification, and effect controls.

The missing architectural layer is not another capability and not primarily a larger model.

Friday lacks a durable, typed representation of **the work currently being performed**.

Today, a large amount of cross-capability coordination is reconstructed repeatedly from conversation text, recent messages, local flags, route-specific heuristics, model prompts, and tool output prose. The model is therefore asked to act simultaneously as:

- intent classifier;
- reference resolver;
- workflow planner;
- tool dispatcher;
- error interpreter;
- completion judge;
- answer author;
- checker of its own answer.

A stronger model will improve each local decision, but it will not remove the compound reliability problem. A chain containing many individually probable decisions still fails often enough to feel arbitrary to the user.

The recommended missing layer is an **Interaction Control Plane with Operational Memory**.

Its central data structure is a lightweight durable **Work Item** that records:

```text
what the user is trying to accomplish
which conversation and earlier work it belongs to
which people, documents, dates, and sources are currently in scope
which steps have completed
which results are partial, ambiguous, stale, or unavailable
what still needs to happen
what counts as completion
what may be published to the user
```

The target flow is:

```text
User message
    -> turn interpretation and continuation resolution
    -> Work Item / Active Frame
    -> code-owned Playbook or validated WorkGraph
    -> typed Capability Steps
    -> Execution Kernel
    -> Outcome Ledger
    -> Completion Gate
    -> bounded synthesis
    -> verified publication
```

The model should understand meaning, propose plans, compare evidence, and express results. The system should retain operational state, enforce step contracts, classify failures, decide whether the task is complete, and preserve continuity after a follow-up or interruption.

The key architectural shift is:

```text
Current:
    capabilities are connected by model improvisation

Target:
    capabilities are connected by a durable WorkGraph
    the model proposes and interprets
    the system owns state and completion
```

## Problem statement

Friday can perform many tasks in isolation and still appear unexpectedly incapable during an ordinary multi-step interaction.

Typical examples include:

```text
find a document
    -> select the correct result
    -> read the relevant passage
    -> explain its contents

find what was decided in a conversation
    -> locate the related document
    -> compare it with current external information

review an uploaded file
    -> accept a correction
    -> preserve the unresolved second half of the user's message

search the web
    -> detect that one provider failed
    -> continue with another source
    -> distinguish partial evidence from task completion
```

Each individual organ may behave correctly. The overall task can still fail because the system loses or inconsistently reconstructs:

- the active user goal;
- the relationship between the current message and the previous turn;
- the selected candidate;
- the reason a tool was called;
- the difference between a successful tool call and a completed user task;
- unresolved ambiguity;
- partial coverage;
- the correct next step;
- the effect of a previous failure;
- the exact claims that are safe to publish.

This creates the characteristic failure mode:

> The system has enough information internally, but the visible answer is wrong, incomplete, contradictory, or unrelated.

The repository already contains regression tests that describe this class directly. In [`tests/test_a_structural_outcome_speaks_for_itself.py`](../tests/test_a_structural_outcome_speaks_for_itself.py), the tested defect is that the system has reached a deterministic result while the model's final prose contradicts it. The corrective principle is that structure owns settled claims and the model receives only the unresolved remainder.

A second family appears in [`tests/test_a_short_follow_up_still_asks_about_the_person.py`](../tests/test_a_short_follow_up_still_asks_about_the_person.py):

```text
What did Yato write?
    -> And Pegasus?
```

The second message omits the verb, corpus, time scope, and operation. A person understands it as a continuation. Friday currently requires several separate mechanisms to recover that continuity and prevent the name from being routed into an unrelated external web search.

These are not isolated prompt defects. They indicate that operational continuity is not a first-class system object.

## Current architecture: strong turn contracts, weak cross-turn work state

The V12 work is moving in the correct direction.

[`friday/orchestration/contracts.py`](../friday/orchestration/contracts.py) defines a closed `TurnPlan` containing:

```text
route
objective
evidence_requests
tool_intents
output contract
confidence
fallback
reason code
```

[`friday/orchestration/planner.py`](../friday/orchestration/planner.py) restricts the planner to producing exactly that typed object. It cannot read files, authorize users, execute tools, or answer the user.

[`friday/orchestration/router.py`](../friday/orchestration/router.py) correctly preserves one runtime owner, uses effect-free preparation, rechecks authority, and does not retry through legacy after a promoted handler has started.

These are valuable guarantees.

However, the current `TurnPlan` remains a **single-turn plan**. It does not record:

```text
persistent work item identity
workflow state
completed steps
selected candidates
unresolved references
expected postconditions
recovery state
continuation policy
completion criteria
```

It answers:

> What should happen during this turn?

It does not yet answer:

> Which ongoing piece of work does this turn belong to, what has already happened, and what must still happen before the user's goal is satisfied?

The route model is also intentionally narrow for current canary safety:

- `file_read` uses attached-file and conversation evidence;
- `archive_read` uses archive and conversation evidence;
- `web_read` uses web and conversation evidence;
- effect routes remain legacy-owned;
- promoted read routes currently accept no model-owned tool steps.

That is an appropriate migration boundary. It is not yet a composition architecture.

A request such as:

```text
Compare this uploaded agreement with the current public rules.
```

requires at least two evidence branches:

```text
read attached file
search and verify current web sources
```

followed by a comparison step. It cannot be represented as one exclusive source route without either broadening the route beyond its safe contract or performing a second independently coordinated turn.

The system therefore needs a level above individual route execution.

## The missing memory type

Friday already has several forms of memory:

| Memory type | Purpose | Current state |
|---|---|---|
| Semantic memory | Facts, documents, entities, relationships | Substantial |
| Episodic memory | Messages and conversation history | Present, retrieval improvements planned |
| Procedural memory | Tools, handlers, prompts, policies, mission logic | Present but distributed |
| Operational memory | Current goal, selected objects, completed steps, unresolved work, completion state | Missing as a unified first-class layer |

Operational memory is not chain-of-thought.

It must not store private model reasoning or hidden deliberation. It stores inspectable task state:

```text
accepted user objective
resolved references
chosen playbook
step statuses
capability outcomes
evidence references
ambiguities
completion decision
published result identity
```

This state should be understandable by code, operators, tests, and future runtime versions without requiring reconstruction of model reasoning.

## Architecture decision

Introduce a model-independent **Interaction Control Plane** between turn interpretation and capability execution.

```text
┌───────────────────────────────────────────────┐
│ Conversation and user message                 │
└──────────────────────┬────────────────────────┘
                       │
                       v
┌───────────────────────────────────────────────┐
│ Turn Interpreter                              │
│                                               │
│ intent proposal                               │
│ continuation proposal                         │
│ reference proposal                            │
│ new-work vs continue-work decision proposal   │
└──────────────────────┬────────────────────────┘
                       │ validated by code
                       v
┌───────────────────────────────────────────────┐
│ Interaction Control Plane                     │
│                                               │
│ Work Item                                     │
│ Active Frame                                  │
│ Playbook / WorkGraph                          │
│ Outcome Ledger                                │
│ Completion Gate                               │
└──────────────────────┬────────────────────────┘
                       │ typed steps
                       v
┌───────────────────────────────────────────────┐
│ Capability Layer                              │
│                                               │
│ document search and read                      │
│ message search                                │
│ web research                                  │
│ graph and entity lookup                       │
│ parsing, generation, reminders, MCP, etc.     │
└──────────────────────┬────────────────────────┘
                       │
                       v
┌───────────────────────────────────────────────┐
│ Execution Kernel and data/effect plane        │
│                                               │
│ authority, budgets, effects, provenance       │
│ deadlines, idempotency, audit                 │
└──────────────────────┬────────────────────────┘
                       │ structured outcomes
                       v
┌───────────────────────────────────────────────┐
│ Completion and publication                    │
│                                               │
│ Outcome Bundle                                │
│ synthesizer                                   │
│ verifier                                      │
│ transport-neutral renderer                    │
└───────────────────────────────────────────────┘
```

The Interaction Control Plane must not replace:

- permissions;
- storage;
- retrieval;
- Execution Kernel;
- missions;
- model profiles;
- security labels;
- provenance;
- verification.

It coordinates them.

## Three scales of work

Not every message should create a database workflow. The architecture should distinguish three scales.

### 1. Direct Turn

A direct turn is appropriate for:

```text
greeting
small talk
simple rewrite
self-contained general explanation
one-step deterministic command
```

A direct turn may use a bounded transient plan and produce one answer without durable work state.

### 2. Work Item

A Work Item is appropriate for most meaningful interactive tasks:

```text
locate a document
explain a located document
recall a conversation
continue a previous question
compare two sources
perform web research
review a document
find evidence and generate a report
recover from a partial tool failure
```

A Work Item commonly spans:

- multiple capability calls;
- multiple model stages;
- multiple user messages;
- an ambiguity-resolution pause;
- a short interruption or retry.

It is durable enough to preserve continuity and inspectable state, but lighter than a mission.

### 3. Mission

A Mission remains appropriate for:

```text
long-running background research
multi-hour or multi-day work
explicit autonomous execution
scheduled or worker-driven activity
large task graphs with recovery and review
```

Friday already has a substantial mission model in [`docs/EXECUTIVE.md`](../docs/EXECUTIVE.md): missions, mission tasks, dependencies, statuses, recovery of stale running tasks, bounded tool loops, and Inbox-gated results.

The Work Item design should extract and reuse common workflow semantics rather than create an unrelated second orchestration system.

## Work Item contract

A Work Item represents the user's current job, not merely the current message.

Illustrative contract:

```python
@dataclass(frozen=True, slots=True)
class WorkItem:
    id: str
    tenant_id: str
    actor_id: str
    conversation_id: str

    kind: WorkKind
    goal: str
    state: WorkState

    playbook_id: str | None
    graph_revision: int
    active_step_id: str | None

    source_scope: tuple[SourceScope, ...]
    active_subjects: tuple[SubjectRef, ...]
    temporal_constraints: tuple[TemporalConstraint, ...]
    selected_sources: tuple[SourceRef, ...]

    unresolved_questions: tuple[WorkQuestion, ...]
    completion_contract: CompletionContract
    output_contract: WorkOutputContract

    security_label_id: str
    created_at: str
    updated_at: str
    expires_at: str | None
    completed_at: str | None
```

The `goal` is the accepted user objective. It must be concise and stable enough to survive several turns.

Examples:

```text
Find the approximate May document about backup failures and explain its conclusion.

Determine what Yato and Pegasus said about the deployment and compare their positions.

Read the attached contract and compare its termination clause with current public guidance.
```

The model may propose a goal. The controller validates and persists it. Later turns may amend the goal only through an explicit state transition.

## Work state machine

Recommended states:

```text
new
active
waiting_for_input
waiting_for_capability
ready_to_answer
completed
blocked
partial
uncertain
failed
cancelled
expired
```

Suggested transitions:

```text
new
  -> active

active
  -> waiting_for_input
  -> waiting_for_capability
  -> ready_to_answer
  -> partial
  -> blocked
  -> uncertain
  -> failed
  -> cancelled

waiting_for_input
  -> active
  -> cancelled
  -> expired

waiting_for_capability
  -> active
  -> partial
  -> uncertain
  -> failed

ready_to_answer
  -> completed
  -> active          # verifier requests another bounded step
  -> partial
  -> failed

partial
  -> active          # user requests continuation or fallback
  -> completed       # user accepts partial result
  -> cancelled
```

`uncertain` is required when an operation may have occurred but the system cannot prove its postcondition.

`partial` is required when valid evidence exists but the completion contract is not fully satisfied.

`no result` is not a state by itself. The outcome must explain whether the cause is:

```text
no matching source
index incomplete
permission boundary
capability unavailable
ambiguous query
unsupported source
expired deadline
```

## Work Step contract

A WorkGraph contains typed steps.

```python
@dataclass(frozen=True, slots=True)
class WorkStep:
    id: str
    work_item_id: str
    kind: str
    capability: str | None

    depends_on: tuple[str, ...]
    status: StepStatus

    input_refs: tuple[str, ...]
    expected_output_type: str
    preconditions: tuple[Condition, ...]
    postconditions: tuple[Condition, ...]

    retry_policy: RetryPolicy
    fallback_policy: FallbackPolicy
    deadline_at: str | None

    outcome_id: str | None
    attempt: int
```

Recommended statuses:

```text
pending
ready
running
done
partial
needs_input
unavailable
policy_denied
uncertain
failed
skipped
cancelled
```

A step becoming `done` means that its postconditions were satisfied. It does not mean the Work Item is complete.

## Active Frame

Conversation history is evidence, not the sole source of operational state.

Each active Work Item should maintain a small typed Active Frame:

```python
@dataclass(frozen=True, slots=True)
class ActiveFrame:
    work_item_id: str
    active_goal: str

    people: tuple[EntityRef, ...]
    organizations: tuple[EntityRef, ...]
    documents: tuple[DocumentRef, ...]
    conversations: tuple[ConversationRef, ...]

    temporal_constraints: tuple[TemporalConstraint, ...]
    source_scope: tuple[SourceScope, ...]

    candidate_set_id: str | None
    selected_candidate_id: str | None
    last_successful_step_id: str | None

    pending_question_id: str | None
    unresolved_references: tuple[ReferenceSlot, ...]
```

This frame allows Friday to interpret short continuations without rebuilding the whole task from prose.

Examples:

```text
User: And for April?

Operational interpretation:
    update the active temporal constraint
    invalidate only affected search and ranking steps
    preserve the goal, source corpus, people, and output contract
```

```text
User: The second one.

Operational interpretation:
    select candidate 2 from the current candidate set
    continue at the read-and-explain step
```

```text
User: Check it on the web now.

Operational interpretation:
    preserve the internal claim being checked
    add a public-evidence verification branch
    do not discard the document evidence already collected
```

The model may propose these interpretations. The Work Coordinator validates them against the current frame.

## Continuation and reference resolution

Continuation resolution should be explicit.

The interpreter proposes one of:

```text
new_work
continue_active_work
resume_suspended_work
answer_pending_question
modify_active_constraint
select_candidate
cancel_work
accept_partial_result
```

It may also propose references:

```text
"he" -> entity_pegasus
"the second one" -> candidate_set_17 item 2
"that document" -> selected document in active frame
"for April" -> replacement temporal constraint
"compare with the previous one" -> prior selected source
```

The controller must reject a reference when:

- no compatible slot exists;
- multiple candidates remain equally plausible;
- the referenced object is no longer authorized;
- the candidate set is stale;
- the user changed conversation or security scope in a way that invalidates continuity;
- the proposed reference was not present in accessible context.

A rejected reference should produce a bounded clarification, not an invented selection.

### Continuation confidence is not authority

A model confidence of `0.98` does not authorize document access or prove which candidate the user meant.

Confidence may influence:

- whether to continue automatically;
- whether to ask a clarification;
- whether to show the proposed interpretation in the UI.

It must not bypass:

- authorization;
- candidate identity checks;
- temporal constraints;
- source completeness;
- explicit ambiguity policy.

## Playbooks as procedural memory

Repeated tasks should use code-owned Playbooks rather than asking the model to reinvent the workflow every time.

Initial playbooks:

```text
LocateAndExplainDocument
RecallConversation
ReviewDocument
WebResearch
CompareDocuments
CompareInternalAndExternalSources
FindThenGenerateReport
ResumeInterruptedWork
```

### Example: LocateAndExplainDocument

```text
1. Resolve source scope and approximate temporal constraint.
2. Search the document catalog.
3. Inspect coverage and index status.
4. Evaluate ambiguity.
5. Select a unique candidate or ask the user.
6. Read relevant passages.
7. Check source completeness and authority.
8. Build an Evidence Bundle.
9. Evaluate the completion contract.
10. Synthesize and verify the answer.
```

### Example: RecallConversation

```text
1. Resolve people, conversation scope, and time range.
2. Search message passages.
3. Recover adjacent context.
4. Group hits by conversation episode.
5. Resolve ambiguity.
6. Produce a grounded summary with stable message references.
```

### Example: CompareInternalAndExternalSources

```text
1. Identify the internal claim or document section.
2. Read and verify internal evidence.
3. Construct a public-safe external query.
4. Search and fetch public sources.
5. Evaluate source recency and coverage.
6. Compare claims in a dedicated step.
7. Record agreements, differences, and unresolved points.
8. Complete only when both evidence branches meet their requirements.
```

A Playbook is not a hard-coded answer template. It is a controlled procedure with explicit branch points.

The model remains responsible for semantic tasks such as:

- identifying the relevant clause;
- proposing search terms;
- comparing meaning;
- explaining differences;
- deciding that evidence suggests a new branch.

The system remains responsible for:

- step identity;
- state transitions;
- allowed capabilities;
- branch admission;
- retries;
- completion;
- publication.

## WorkGraph

A Playbook can instantiate a WorkGraph.

```text
                         +------------------+
                         | resolve request  |
                         +---------+--------+
                                   |
                    +--------------+--------------+
                    |                             |
          +---------v----------+        +---------v---------+
          | read internal doc  |        | web research      |
          +---------+----------+        +---------+---------+
                    |                             |
                    +--------------+--------------+
                                   |
                         +---------v---------+
                         | compare evidence  |
                         +---------+---------+
                                   |
                         +---------v---------+
                         | completion gate   |
                         +---------+---------+
                                   |
                         +---------v---------+
                         | synthesize answer |
                         +-------------------+
```

The graph must be:

- acyclic for one execution revision;
- bounded in nodes and depth;
- versioned;
- immutable after admission, except through a validated graph revision;
- explicit about dependencies;
- explicit about which steps may run in parallel;
- explicit about effects;
- recoverable after interruption.

The model may propose graph edits such as:

```text
add verification branch
broaden search window
request clarification
replace unavailable capability with allowed fallback
```

The controller validates the edit and creates a new graph revision.

## Capability contracts

A tool schema describes call syntax. Cross-capability orchestration needs a richer contract.

```python
@dataclass(frozen=True, slots=True)
class CapabilityContract:
    name: str
    purpose: str

    input_schema: str
    output_schema: str

    required_capabilities: tuple[str, ...]
    accepted_security_levels: tuple[str, ...]

    reads: tuple[str, ...]
    writes: tuple[str, ...]
    effect: ToolEffect

    latency_class: str
    cost_class: str
    retry_semantics: str
    idempotency_semantics: str
    resume_semantics: str

    preconditions: tuple[str, ...]
    postconditions: tuple[str, ...]
    failure_classes: tuple[str, ...]
```

Example:

```text
Capability: locate_document

Purpose:
    identify document candidates matching a query and constraints

Produces:
    candidate set
    match explanations
    corpus coverage status
    ambiguity status

Does not produce:
    verified document contents
    completed answer to a content question

Failure classes:
    no_match
    ambiguous
    index_partial
    index_stale
    policy_denied
    unavailable
```

This distinction is essential:

> A successful capability call is not necessarily a successfully completed user task.

## Structured CapabilityOutcome

Capabilities should not return arbitrary prose that the next model must interpret.

Recommended outcome:

```python
@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    id: str
    capability: str
    status: OutcomeStatus

    value_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    coverage: CoverageReport
    ambiguity: tuple[Ambiguity, ...]
    warnings: tuple[OutcomeWarning, ...]

    retryable: bool
    retry_after: str | None
    next_options: tuple[str, ...]

    started_at: str
    completed_at: str
    input_digest: str
    output_digest: str
```

Recommended statuses:

```text
success
partial
no_match
ambiguous
needs_input
unavailable
policy_denied
invalid_input
stale
uncertain
failed
cancelled
```

The human-readable message is a projection of this object, not the object itself.

### Coverage is first-class

A search outcome should distinguish:

```text
complete corpus searched
lexical only
semantic index incomplete
passage index stale
candidate cap reached
pending sources excluded
restricted sources excluded by policy
```

A parser outcome should distinguish:

```text
complete extraction
page cap reached
OCR advisory
unsupported embedded object
archive truncated
```

A web outcome should distinguish:

```text
multiple independent sources
single-source result
freshness unavailable
provider unavailable
fetch blocked
partial page extraction
```

The completion gate can then make deterministic decisions about whether the task is sufficiently supported.

## Outcome Ledger

Every Work Item should maintain an append-only logical ledger of accepted outcomes and transitions.

```text
work created
continuation bound
constraint changed
step admitted
capability started
capability outcome accepted
candidate selected
clarification requested
completion evaluated
answer published
work completed
```

The ledger should store:

- object IDs;
- state transition kinds;
- digests;
- closed reason codes;
- capability and step identity;
- evidence references;
- timestamps;
- policy decisions.

It should not store hidden chain-of-thought.

Sensitive bodies remain in their normal protected stores and are referenced by stable IDs.

The ledger provides:

- recovery after process failure;
- explanation of why a step happened;
- episode-level debugging;
- audit of completion decisions;
- deterministic replay of control state;
- migration between runtime versions.

## Completion Contract and Completion Gate

The system must explicitly define when a user goal is complete.

Illustrative completion contract:

```python
CompletionContract(
    required_conditions=(
        "candidate_selected",
        "selected_source_read",
        "source_coverage_acceptable",
        "content_question_answered",
        "citations_available",
    ),
    allow_partial=True,
    require_user_selection_when_ambiguous=True,
)
```

The Completion Gate evaluates:

```text
Were all required evidence branches completed?
Was candidate ambiguity resolved?
Was the source actually read, not merely located?
Is source coverage sufficient?
Did an effect reach a proven postcondition?
Are citations and provenance available?
Is the output format satisfied?
Are unresolved questions still present?
```

Only then may the Work Item enter `ready_to_answer`.

Possible decisions:

```text
continue
ask_user
use_allowed_fallback
return_partial
abstain
ready_to_answer
```

The model may recommend a decision. The gate owns it.

### False completion is a primary defect class

The system must prevent:

```text
tool returned a string
    -> step marked successful
    -> work declared complete
```

The mission subsystem already demonstrates the correct principle: an unavailable model or capability is a failed step, not a valid produced result. The same semantics should apply to ordinary interaction.

## Outcome Bundle and synthesis

The final model should not receive a loose mixture of full conversation history, unrelated retrieval hints, tool error strings, stale candidates, and system instructions.

It should receive a compact typed Outcome Bundle:

```python
@dataclass(frozen=True, slots=True)
class OutcomeBundle:
    work_item_id: str
    goal: str
    completion_status: str

    completed_steps: tuple[StepSummary, ...]
    claims: tuple[SupportedClaim, ...]
    evidence_refs: tuple[str, ...]

    completed_actions: tuple[ActionResult, ...]
    incomplete_actions: tuple[ActionResult, ...]

    uncertainties: tuple[Uncertainty, ...]
    coverage: tuple[CoverageReport, ...]
    citations: tuple[CitationRef, ...]

    output_contract: WorkOutputContract
```

The synthesizer's task becomes:

> Express this accepted outcome clearly, without adding unsupported claims or changing settled state.

A verifier checks:

- every source-backed claim maps to accepted evidence;
- the answer does not state that an incomplete action succeeded;
- ambiguities are disclosed;
- partial coverage is visible;
- structural outcomes are not contradicted;
- the response satisfies the requested format.

Where possible, deterministic rendering should own settled operational statements:

```text
The document was saved.
The reminder was created.
Access was denied.
Three candidate documents were found.
The search index is incomplete.
```

The model may explain or contextualize these statements, but it must not rewrite their truth value.

## Failure and recovery semantics

A Work Item should survive local failures without forgetting its goal.

### Failure classes

At minimum:

```text
interpretation_failed
reference_unresolved
planning_failed
capability_unavailable
capability_timeout
capability_protocol_error
no_match
ambiguous_result
coverage_insufficient
policy_denied
postcondition_unknown
verification_failed
publication_failed
state_conflict
```

### Retry policy

Automatic retry is allowed only when the capability contract declares it safe.

Examples:

```text
read-only transient transport failure
    -> retry or allowed fallback

policy denial
    -> never retry through another path

unknown mutating effect
    -> reconcile postcondition before any retry

ambiguous document selection
    -> ask the user, do not guess
```

### Resume semantics

After restart, Friday should be able to state:

```text
The document search completed.
Three candidates remain.
You were asked to choose one.
```

It should not reconstruct this state from the last assistant paragraph.

### Invalidation

When the user changes a constraint, the controller should invalidate only affected steps.

Example:

```text
Original work:
    documents from May about backups

User:
    I meant April.

Invalidate:
    document search
    candidate ranking
    downstream selected-source steps

Preserve:
    goal kind
    source corpus
    topic
    output contract
```

This is more reliable and cheaper than restarting the whole task.

## Clarification policy

Clarifications should be treated as workflow states, not conversational accidents.

A `waiting_for_input` Work Item stores:

```text
question id
question type
allowed answer shape
candidate set or missing field
expiry
```

Examples:

```text
Which of these three documents did you mean?

Did you mean the date the file was received or the date written inside it?

Should I compare against public web sources, or only the local archive?
```

When the next message arrives, the controller first attempts to satisfy the pending question.

The model does not need to infer from scratch why Friday asked it.

## Context assembly

The model should receive context selected from the active Work Item, not an ever-growing transcript dump.

Recommended layers:

```text
1. Stable system and policy contract
2. Current Work Item goal and state
3. Active Frame
4. Current pending question or step
5. Accepted evidence needed for this stage
6. Bounded recent conversational wording
```

The planner, capability interpreter, comparer, synthesizer, and verifier need different projections.

They should not all receive the same giant prompt.

### Context ownership

The model may not silently replace stored operational state with a reinterpretation of old prose.

When conversation text conflicts with accepted state:

- explicit new user correction may amend state;
- model speculation may not;
- stale assistant text is not authority;
- deterministic outcomes remain authoritative until corrected through a valid transition.

## Integration with V12

V12 should remain the semantic planning layer, but `TurnPlan` should become one input to a durable Work Coordinator rather than the entire coordination mechanism.

### Near-term integration

Keep `friday.turn-plan.v1` unchanged for current canary safety.

Add a code-owned interpretation layer around it:

```text
TurnPlan
    -> create direct turn
    -> create Work Item
    -> continue Work Item
    -> answer pending Work Item question
    -> reject and fall back
```

The decision is validated against:

- conversation state;
- active Work Items;
- security scope;
- candidate sets;
- current route support;
- deadlines.

### Later Work Directive contract

A future version may introduce a separate closed contract rather than overloading `TurnPlan`:

```python
WorkDirective(
    action="create|continue|modify|select|suspend|complete|cancel",
    work_item_ref="active|none|opaque-id",
    proposed_goal="...",
    reference_updates=[...],
    proposed_steps=[...],
    requested_clarification=None,
    reason_code="...",
)
```

The model describes intent. It never mutates Work Items directly.

### WorkGraph composition above route handlers

Current V12 read handlers should remain narrow.

The Work Coordinator composes them:

```text
WorkGraph step: read attached source
    -> FILE_READ handler

WorkGraph step: retrieve archived source
    -> ARCHIVE_READ handler

WorkGraph step: gather public sources
    -> WEB_READ handler
```

A mixed task is completed by a graph containing multiple separately admitted route steps, not by making one route universally powerful.

### One effect owner remains mandatory

The WorkGraph does not weaken the current invariant:

> One request and one effect have exactly one owner.

Effect steps still require:

- Execution Kernel admission;
- approval where required;
- idempotency;
- postcondition checks;
- no unsafe legacy retry after uncertain execution.

## Integration with Missions

Work Items and Missions should share primitives while retaining different product semantics.

| Property | Work Item | Mission |
|---|---|---|
| Typical duration | Seconds to a short interactive session | Minutes to days |
| User interaction | Frequent | Optional or intermittent |
| Autonomy | Low and bounded | Explicitly configurable |
| Graph size | Small | Potentially larger |
| Background execution | Limited | Primary feature |
| Clarification | Common | May block mission |
| Completion | Immediate user goal | Mission objective and produced artifacts |

Shared primitives should include:

```text
WorkStep state model
CapabilityOutcome
retry and uncertainty semantics
dependency validation
postconditions
Outcome Ledger
security scope
provenance
```

Avoid two independent definitions of:

```text
running
failed
partial
uncertain
retryable
completed
```

A Mission may contain or spawn Work Items for interactive subproblems. A Work Item may be promoted into a Mission when the user asks Friday to continue in the background.

## Integration with document and message retrieval

The recommendations in [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md) fit naturally into this plane.

`archive_search` should return a structured CapabilityOutcome containing:

```text
selected corpora
parsed temporal constraint
candidate set id
candidate references
match channels
coverage status
index health
ambiguity
```

A candidate set should be durable enough for follow-ups such as:

```text
the second one
show the older one
only the pending files
now search the messages instead
```

Reading a candidate is a separate step from locating it.

Explaining content is a separate completion condition from reading it.

The Work Item prevents these stages from collapsing into one vague `search succeeded` flag.

## Integration with web research

Web research should become a playbook-backed workflow, not merely a tool invocation.

```text
formulate query
search
select sources
fetch
check recency and domains
extract evidence
compare sources
assess coverage
synthesize
```

Provider failure should be represented as capability state.

The model must not be told that search succeeded when it received an error string. Existing tests already enforce this locally. The Interaction Control Plane generalizes that rule across all capabilities.

## Integration with security and assurance

The Work Item, steps, outcomes, candidate sets, and Outcome Bundle must carry or reference the effective security label described in [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md).

The Interaction Control Plane must not become a path around:

- channel policy;
- derivative labels;
- source authorization;
- purpose restrictions;
- external-tool denial;
- controlled export.

A WorkGraph branch may be rejected because its evidence cannot cross into the requested capability.

Example:

```text
restricted document evidence
    + requested public web comparison
```

The controller must separate a public-safe external question or deny the branch. It must never serialize the active restricted frame into a web query.

The assurance matrix in [`SYSTEM_ASSURANCE_AND_RECORDS_GOVERNANCE.md`](SYSTEM_ASSURANCE_AND_RECORDS_GOVERNANCE.md) should gain claims for operational continuity:

```text
A completed Work Item satisfies its completion contract.
A failed capability cannot be represented as a successful step.
A continuation cannot bind to an unauthorized object.
A published answer corresponds to one accepted Outcome Bundle.
A restart does not erase an unresolved user task.
```

## Observability: Turn Trace and Episode Trace

Before implementing the full plane, Friday should record a privacy-safe structural trace for every non-trivial turn.

Suggested trace fields:

```text
turn id
conversation id hash
work item id
new vs continued work
resolved intent class
continuation kind
selected playbook
step ids and capability names
outcome statuses
completion decision
publication status
closed failure reason
latency and budget consumption
```

Do not include:

- raw user text;
- document titles;
- search queries;
- model prose;
- evidence bodies;
- private paths.

The trace should allow every observed "Friday became confused" episode to be classified into one stage:

```text
intent failure
continuation failure
reference failure
planning failure
candidate-generation failure
capability failure
state-loss failure
completion failure
synthesis contradiction
publication failure
```

Today, these different defects often look identical to the user.

## Evaluation must use episodes, not isolated prompts

Single-turn benchmarks do not measure functional coherence.

The primary test unit should be an episode:

```text
initial request
first result
short follow-up
constraint change
capability failure
resume
final answer
```

Important episode examples:

```text
Find the backup document from around May.
    -> The second one.
    -> What does it conclude?
    -> Now compare that with our messages.

What did Yato say about deployment?
    -> And Pegasus?
    -> Which one changed their position later?

Review this file and remember that I do not want greetings.
    -> The file review remains complete.
    -> The rule is persisted.
    -> Neither half of the request is lost.

Research current guidance.
    -> One provider fails.
    -> Friday continues with allowed alternatives.
    -> It reports partial coverage rather than fabricated completeness.
```

Recommended metrics:

```text
task completion rate
follow-up binding accuracy
cross-capability composition success
false completion rate
false absence rate
state-loss rate
resume-after-failure success
clarification precision
unsupported-claim rate
wrong-corpus rate
average unnecessary capability calls
```

The most revealing follow-ups are:

```text
and the second one?
and for last year?
check it on the web now
compare it with that document
no, I meant the conversation
continue
use the previous result
```

## Implementation plan

### P0: structural Turn Trace

Add privacy-safe structural tracing before major behavior changes.

Goals:

- classify current failure stages;
- establish a baseline;
- identify which route classes most often lose continuity;
- measure false completion and state loss.

### P1: CapabilityOutcome contract

Standardize outcomes for the highest-value read capabilities:

```text
archive_search
document_read
message_search
web_research
entity_lookup
```

Do not require all legacy tools to migrate at once.

### P2: lightweight durable Work Item

Implement:

- `work_items` storage;
- one active Work Item per conversation by default;
- explicit suspended items;
- goal, state, active frame, completion contract;
- binding of short follow-ups;
- expiry and cancellation.

Keep ordinary direct turns outside this path.

### P3: candidate sets and pending questions

Make candidate selection durable:

```text
candidate_set
ordered items
query and constraint digest
coverage status
created_at
expires_at
```

Add typed `waiting_for_input` questions.

### P4: first Playbooks

Implement five high-value playbooks:

```text
LocateAndExplainDocument
RecallConversation
WebResearch
CompareInternalAndExternalSources
ReviewDocument
```

Each playbook must have explicit completion conditions.

### P5: Outcome Ledger and Completion Gate

Persist accepted outcomes and evaluate readiness independently of the synthesizer.

The first release can use code-owned completion rules rather than a generic rule engine.

### P6: Outcome Bundle and bounded synthesis

Replace loose final prompt assembly on promoted workflows with an Outcome Bundle.

Add contradiction checks between structural outcomes and model prose.

### P7: V12 Work Directive and WorkGraph planning

Allow the attested V12 model to propose:

```text
create work
continue work
modify a constraint
select a candidate
add a bounded branch
request clarification
complete
```

All proposals remain code-validated.

### P8: unify workflow primitives with Executive

Extract common step status, outcome, retry, uncertainty, and dependency contracts.

Do not rewrite Missions before the Work Item path proves useful.

### P9: decompose legacy orchestration

Move route-specific coordination out of the large legacy runtime behind stable interfaces.

The target is not to create another mega-orchestrator. The Interaction Control Plane should reduce central branching over time.

## Suggested storage projection

Illustrative tables:

```text
work_items
    id
    tenant_id
    actor_id
    conversation_id
    kind
    goal
    state
    playbook_id
    graph_revision
    active_step_id
    security_label_id
    completion_contract_json
    output_contract_json
    created_at
    updated_at
    expires_at
    completed_at

work_steps
    id
    work_item_id
    graph_revision
    kind
    capability
    status
    dependency_ids_json
    input_refs_json
    expected_output_type
    retry_policy_json
    fallback_policy_json
    attempt
    outcome_id
    created_at
    updated_at

work_outcomes
    id
    work_item_id
    step_id
    capability
    status
    value_refs_json
    evidence_refs_json
    coverage_json
    ambiguity_json
    warnings_json
    retryable
    input_digest
    output_digest
    created_at

work_events
    seq
    work_item_id
    event_kind
    reason_code
    object_ref
    event_digest
    created_at

candidate_sets
    id
    work_item_id
    source_kind
    constraint_digest
    coverage_json
    created_at
    expires_at

candidate_set_items
    candidate_set_id
    ordinal
    object_ref
    score
    explanation_json

work_questions
    id
    work_item_id
    question_kind
    allowed_answer_shape_json
    state
    created_at
    expires_at
    answered_at
```

Bodies and sensitive evidence remain in existing stores. These tables contain operational references and bounded structured projections.

## Architectural invariants

1. **A model never directly mutates Work Item state.**
2. **A successful capability call does not imply completed user work.**
3. **Every completed Work Item satisfies an explicit Completion Contract.**
4. **Every published workflow answer is derived from one accepted Outcome Bundle.**
5. **A follow-up binds only to an authorized, current operational reference.**
6. **Ambiguity is stored and resolved, never silently discarded.**
7. **Partial coverage remains visible through completion and publication.**
8. **A failed capability cannot become answer evidence merely because it returned text.**
9. **Unknown mutating effects enter `uncertain` and require reconciliation.**
10. **Restarting Friday does not erase active interactive work.**
11. **Operational memory stores state, not hidden chain-of-thought.**
12. **Security labels and channel restrictions apply to Work Items and every outcome.**
13. **One effect and one user publication retain exactly one owner.**
14. **Work Item and Mission workflow semantics do not diverge silently.**
15. **A larger model may increase semantic authority only within measured model profiles, never state or policy authority.**

## Anti-patterns to avoid

### One universal agent loop

Do not respond by giving a larger model every tool and asking it to continue until satisfied.

This preserves the original problem:

- state remains implicit;
- completion remains subjective;
- failure semantics remain textual;
- retries remain unsafe;
- debugging remains difficult.

### Persisting the whole prompt as operational memory

Conversation text and prompts are not a stable task state format.

They contain:

- stale assistant claims;
- user corrections;
- unrelated turns;
- private data;
- model-specific formatting;
- instructions that may no longer apply.

### Converting every turn into a Mission

This creates unnecessary storage, latency, UI clutter, and lifecycle complexity.

Use Direct Turns, Work Items, and Missions at different scales.

### Allowing free-form tool result interpretation

A prose result such as "nothing found" is insufficient. The controller needs structured status and coverage.

### Treating model confidence as completion

Model confidence is advisory. Completion is a contract over accepted outcomes and postconditions.

### Hiding uncertainty to preserve conversational smoothness

A fluent wrong completion is worse than a concise visible partial result.

### Creating a second orchestration stack beside V12 and Executive

The Interaction Control Plane should unify existing direction, not add another isolated runtime family.

## Acceptance criteria

The first useful release should demonstrate:

### Continuation

- `And Pegasus?` continues the previous person-activity question.
- `The second one` selects from the active candidate set.
- `For April instead` updates the active temporal constraint and reruns only affected steps.
- A follow-up never binds to an unauthorized or expired candidate.

### Cross-capability composition

- Friday can read an attached document and compare it with public web evidence in one Work Item.
- Friday can locate a conversation, then locate a related document, without losing the original goal.
- A failed web branch produces a partial comparison rather than a fabricated complete answer.

### Completion

- Locating a document does not complete a request to explain its content.
- A parser partial result cannot satisfy a full-read completion condition.
- An ambiguous candidate set moves to `waiting_for_input`.
- An unavailable capability cannot be stored as a successful result.

### Recovery

- Restarting the service preserves an unresolved candidate selection.
- A stale running read step is safely retried according to its contract.
- An uncertain mutating step is reconciled rather than blindly repeated.

### Publication

- The final answer matches one accepted Outcome Bundle.
- Structural success, denial, and failure states cannot be contradicted by model prose.
- Partial coverage and unresolved uncertainty remain visible.
- Exactly one user-visible publication occurs.

## Suggested executable regression tests

```text
test_a_short_follow_up_continues_the_active_work_item.py
test_the_second_candidate_is_selected_without_a_new_search.py
test_a_changed_date_invalidates_only_affected_steps.py
test_a_candidate_set_cannot_cross_an_authority_boundary.py
test_locating_a_document_does_not_complete_an_explanation.py
test_a_partial_parse_cannot_satisfy_full_read_completion.py
test_a_failed_capability_is_never_answer_evidence.py
test_an_ambiguous_result_waits_for_user_input.py
test_a_work_item_survives_process_restart.py
test_a_mixed_document_and_web_task_uses_two_evidence_branches.py
test_one_failed_branch_produces_a_visible_partial_outcome.py
test_the_model_cannot_mutate_work_state_directly.py
test_every_completed_work_item_satisfies_its_contract.py
test_one_outcome_bundle_produces_one_publication.py
test_structural_outcomes_cannot_be_contradicted_by_synthesis.py
test_operational_memory_contains_no_chain_of_thought.py
```

## Priority recommendation

Do not begin by implementing a generic autonomous WorkGraph engine.

The highest-value sequence is:

```text
1. Structural Turn Trace
2. CapabilityOutcome for document, message, and web retrieval
3. Lightweight Work Item and Active Frame
4. Durable candidate sets and pending questions
5. LocateAndExplainDocument and RecallConversation playbooks
6. Completion Gate
7. Outcome Bundle synthesis
8. V12 Work Directive and mixed-source WorkGraphs
```

This sequence improves the exact user-visible failures already observed while preserving the safe incremental V12 migration.

## Final assessment

Friday does not primarily fail because the current 27B model is unintelligent.

It fails because an intelligent but probabilistic component is currently asked to carry too much implicit coordination across too many stages.

A 120B+ model will:

- understand more follow-ups;
- produce better plans;
- compare evidence more accurately;
- write better answers.

It will not reliably preserve:

- the authoritative current task state;
- which steps truly succeeded;
- which ambiguity remains;
- what must happen before completion;
- whether a retry is safe;
- whether a structural outcome may be contradicted.

Those are system responsibilities.

The missing connective tissue is therefore:

```text
Operational Memory
    +
Work Items
    +
Playbooks and WorkGraphs
    +
Capability Outcomes
    +
Completion Gates
```

With this layer, a model mistake becomes a bounded proposal or one failed step. It no longer has to collapse the whole user workflow or erase the purpose of the conversation.

The intended product experience becomes:

```text
Friday remembers not only what the user knows,
but also what the two of them are currently doing together.
```
