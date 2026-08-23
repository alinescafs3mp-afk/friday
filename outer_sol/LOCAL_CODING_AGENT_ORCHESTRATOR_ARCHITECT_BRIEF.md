# Local Coding Agent Orchestrator: Architect Implementation Brief

> Document ID: FRIDAY-DEV-AGENT-001  
> Status: External architecture handoff, draft v0.1  
> Date: 23 August 2026  
> Observed Friday repository checkpoint: `main` at `141c2cf0139f116ff9fa468132d802445508f3f9`  
> Observed Friday production checkpoint: Friday `0.207.4` / `c91260d6f8f74e3276851ebfd42916a2af4396db`, schema 38  
> Audience: Friday system architect and implementation lead  
> Scope: a small local-development CLI that uses OpenCode as the coding-agent engine and one future OpenAI-compatible local model endpoint, initially expected to be GLM-5.2-class  
> Product boundary: this is an out-of-band development tool. Its implementation is not Friday product progress and must not interrupt the active golden-journey/retrieval convergence course.

## How to use this brief

This document asks for a separate, narrow tool named `friday-dev`. It does **not** ask for another general autonomous-agent platform.

Before implementation:

1. Re-read the current Friday canonical status.
2. Confirm the current OpenCode CLI/config interfaces against one exact version.
3. Pin that version for the first release.
4. Build and test the orchestrator without requiring any real LLM endpoint.
5. Keep the implementation outside the Friday runtime and production release surface.

The observed commits above are only a checkpoint. Current verified source/live identities take precedence if the repository advances.

Official OpenCode interfaces relevant to this brief currently include:

- `opencode run --format json`, `--session`, `--dir`, `--agent`, `--model`, and `--variant`;
- custom OpenAI-compatible providers and configurable `baseURL`;
- `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, and `OPENCODE_CONFIG_CONTENT`;
- custom agents with permissions and bounded `steps`;
- project/global custom tools implemented in TypeScript or JavaScript and backed by scripts in another language;
- a headless HTTP server and OpenAPI interface, which is intentionally deferred from the first orchestrator release.

Reference documentation:

- <https://opencode.ai/docs/cli/>
- <https://opencode.ai/docs/providers/>
- <https://opencode.ai/docs/agents/>
- <https://opencode.ai/docs/custom-tools/>
- <https://opencode.ai/docs/server/>

OpenCode is changing quickly. Do not assume configuration examples in this brief override the schema of the pinned tested version.

## Operator objective

The operator expects to raise one or more local inference endpoints later. The architect should deliver everything else in advance.

The desired operator experience is approximately:

```text
install friday-dev and the pinned OpenCode version
    -> fill in local model endpoint URL and model ID
    -> run a doctor/tool-calling canary
    -> create a task from an architecture brief or plain objective
    -> obtain a separate git worktree
    -> run a read-only planning phase
    -> approve or edit the plan
    -> run an implementation phase
    -> run deterministic gates
    -> run a clean read-only review phase
    -> receive a diff, evidence manifest and handoff
    -> merge/release manually through the existing Friday process
```

The first useful configuration may use the same GLM-5.2-class endpoint for planning, implementation and review. The design must permit later role-specific model profiles without requiring them now.

## Executive decision

Build:

```text
OpenCode
    as the agent loop, context manager, editor, LSP client and terminal UI

friday-dev
    as the project-specific task, worktree, policy, gate and evidence orchestrator
```

Do not build:

```text
a new LLM loop
a new patch engine
a new terminal UI
a new MCP client
a new code index or vector store
a generic WorkGraph
a multi-agent swarm
a daemon or web service in v1
a production deployment controller
```

The core rule is:

> Reuse OpenCode for generic coding-agent mechanics. Own only the Friday-specific control plane.

## Repository and packaging boundary

Create a separate sibling repository or standalone project named `friday-dev`.

Recommended local layout:

```text
<workspace>/
    friday/          # existing product repository
    friday-dev/      # new development orchestrator repository
```

Do not implement the orchestrator inside `friday/`, do not import the Friday runtime as a library, and do not make Friday production releases depend on it.

The Friday repository may later contain one small optional example task/policy document if useful, but the orchestrator must remain independently installable and removable.

Recommended implementation language: Python 3.12 or the currently supported project Python version.

Keep the runtime dependency set deliberately small. Prefer:

- standard-library `subprocess`, `pathlib`, `tomllib`, `json`, `dataclasses`, `fcntl`, and `tempfile`;
- one schema/validation library if it materially improves closed configuration contracts;
- no ORM, database server, web framework, background queue, or plugin marketplace.

Use the `git` and `opencode` executables through explicit argv arrays. Do not introduce GitPython merely to avoid invoking Git.

## Trust model

The local model is treated as capable but fallible, not malicious and not authoritative.

The orchestrator, not the model, owns:

```text
task identity
base commit
worktree identity
allowed write scope
forbidden resources
phase transitions
process timeouts
gate execution
gate interpretation
evidence persistence
final readiness decision
```

OpenCode owns:

```text
model conversation
tool loop
repository exploration
file editing
context compaction
LSP interaction
session storage
raw agent events
```

The human owns:

```text
plan approval
final diff acceptance
commit/merge/push/release approval
production access
endpoint provisioning
```

Passing tests is necessary but never grants the agent permission to merge, push, deploy, restart production services, read live data, or edit outside the task worktree.

## Target architecture

```text
┌───────────────────────────────────────────────────────────────┐
│                         friday-dev CLI                        │
│ task spec · state · worktree · policy · gates · evidence     │
└───────────────┬───────────────────────────────────────────────┘
                │ subprocess argv + JSON events
┌───────────────▼───────────────────────────────────────────────┐
│                    pinned OpenCode CLI                        │
│ agents · tools · edits · LSP · sessions · compaction         │
└───────────────┬───────────────────────────────────────────────┘
                │ OpenAI-compatible API
┌───────────────▼───────────────────────────────────────────────┐
│              operator-provided local endpoint                │
│ initially one GLM-5.2-class model; later replaceable         │
└───────────────────────────────────────────────────────────────┘
```

The first backend adapter should use the OpenCode CLI, not the HTTP server:

```text
opencode run --format json ...
```

Reasons:

- smaller dependency and compatibility surface;
- JSON event capture is sufficient for v1;
- normal OpenCode TUI remains available for manual continuation;
- no server lifecycle, port, authentication, or generated-client management;
- an HTTP/OpenAPI adapter can be added later behind the same backend protocol if repeated need is demonstrated.

## Proposed source layout

The architect may refine names while preserving the boundaries.

```text
friday-dev/
    pyproject.toml
    README.md
    CHANGELOG.md
    LICENSE
    requirements-dev.lock             # or equivalent locked dev environment

    src/friday_dev/
        __init__.py
        cli.py
        errors.py
        exit_codes.py

        config/
            models.py                 # GlobalConfig, ModelProfile, RepoProfile
            load.py
            render.py

        tasks/
            models.py                 # TaskSpec, TaskState, phase enum
            store.py                  # atomic JSON state, locks, permissions
            lifecycle.py
            prompt_bundle.py

        git/
            repository.py
            worktree.py
            diff_scope.py

        opencode/
            backend.py                # protocol/interface
            cli_backend.py
            process.py
            events.py                 # tolerant normalized envelope
            compatibility.py          # exact tested version contract
            config_renderer.py

        policy/
            models.py
            friday.py                 # shipped Friday repo profile
            environment.py            # sanitized environment builder
            command.py                # named safe actions/argv validation

        gates/
            models.py
            runner.py
            friday_profiles.py
            report.py

        evidence/
            models.py
            recorder.py
            manifest.py
            handoff.py

        templates/
            opencode/
                config.json.template
                agents/
                    architect.md
                    implementer.md
                    reviewer.md
                tools/
                    friday_exec.ts
            tasks/
                task.example.toml
            config.example.toml

    tests/
        fixtures/
            fake_opencode.py
            opencode_events/
            tiny_git_repo/
        test_config.py
        test_task_store.py
        test_worktree.py
        test_diff_scope.py
        test_environment.py
        test_command_policy.py
        test_opencode_backend.py
        test_event_parser.py
        test_gate_runner.py
        test_evidence_manifest.py
        test_full_fake_journey.py

    docs/
        ARCHITECTURE.md
        TASK_SPEC.md
        MODEL_ENDPOINTS.md
        SECURITY_BOUNDARY.md
        OPERATOR_RUNBOOK.md
```

No single module should become a second `agent_runtime/__init__.py`. Establish module-size and dependency-direction discipline from the first release.

## Configuration contracts

### `GlobalConfig`

Recommended location:

```text
~/.config/friday-dev/config.toml
```

Candidate shape:

```toml
schema = 1
opencode_bin = "opencode"
opencode_version = "PINNED_AND_TESTED_VERSION"
state_dir = "~/.local/share/friday-dev"
default_model_profile = "local-glm"
default_repo_profile = "friday"

[model_profiles.local-glm]
provider_id = "local"
base_url = "{env:FRIDAY_DEV_LLM_BASE_URL}"
model_id = "{env:FRIDAY_DEV_LLM_MODEL_ID}"
api_key_env = "FRIDAY_DEV_LLM_API_KEY"
wire_api = "chat_completions"
context_tokens = 131072
output_tokens = 16384
tool_calling = true
streaming = true
variant = ""

[repo_profiles.friday]
path = "~/projects/friday"
default_base_ref = "main"
policy = "friday"
```

`wire_api` must support at least:

```text
chat_completions
responses
```

The OpenCode provider renderer should select the provider package/config form required by the pinned version and wire API. Do not make the rest of `friday-dev` depend on those OpenCode-specific details.

Support optional opaque provider/model request settings so endpoint-specific fields can be added without a code release. Validate that secrets are referenced by environment variable name and are never written into generated config, event logs, evidence, or task state.

### `ModelProfile`

Minimum fields:

```text
profile name
provider ID
base URL
upstream model ID
wire API
API-key environment variable name
context limit
output limit
tool-calling declaration
streaming declaration
optional variant/reasoning setting
optional headers/body settings with secret references only
```

One profile may be assigned to all roles:

```text
architect_model = local-glm
implementer_model = local-glm
reviewer_model = local-glm
```

Later profiles may diverge without changing task-state or evidence schemas.

### `RepoProfile`

Minimum fields:

```text
repository path
default base ref
clean-base requirement
forbidden read/write patterns
default allowed write patterns
gate profile names
canonical status files
production-sensitive paths and commands
```

The shipped Friday profile must explicitly protect at least:

```text
.env and secret files
live SQLite and backups
operator/user document roots
production activation directories
systemd/service management
SSH and remote hosts
model-weight and runtime deployment directories
git push and remote ref mutation
Docker in the current no-Docker contour
```

The architect must inspect the current repository and operator tooling rather than guess exact local production paths. Unknown external paths remain inaccessible because the task runs in a worktree with `external_directory` denied and a sanitized environment.

### `TaskSpec`

Recommended on-disk form: TOML for operator readability, parsed into a closed typed model.

Candidate shape:

```toml
schema = 1
id = "2026-08-23-retrieval-identity-001"
title = "Audit SourceRef foundation"
repo_profile = "friday"
base_ref = "main"
mode = "plan_then_implement"
objective = "Audit current identities and implement the smallest safe foundation."
brief_files = [
  "outer_sol/PROJECT_IMPLEMENTATION_STATUS.md",
  "outer_sol/PROJECT_CONVERGENCE_ARCHITECT_BRIEF.md",
]

read_scope = ["**"]
write_scope = [
  "friday/**",
  "tests/**",
  "outer_sol/PROJECT_IMPLEMENTATION_STATUS.md",
]
forbidden_scope = [
  ".env",
  "**/*.sqlite*",
  "models/**",
]

required_gates = ["focused", "static"]
max_agent_steps = 40
max_process_seconds = 7200
max_implementation_attempts = 4

[completion]
require_clean_scope = true
require_plan_approval = true
require_review = true
require_human_finalize = true
```

Task IDs must be filesystem-safe and immutable. A task spec is copied into task state at creation so later edits are visible as revisions rather than silent mutation.

### `TaskState`

Use atomic JSON files rather than introducing SQLite in v1.

Recommended states:

```text
DRAFT
PREPARED
PLANNED
PLAN_APPROVED
IMPLEMENTING
GATE_FAILED
REVIEW_PENDING
READY_FOR_HUMAN
FINALIZED
BLOCKED
CANCELLED
CLEANED
```

Minimum durable fields:

```text
task ID and spec revision
repository canonical path
base ref and resolved base commit
worktree path and branch
phase and phase revision
created/updated timestamps
OpenCode version
model profile identities without secrets
OpenCode session IDs per role
run IDs and attempt numbers
raw event artifact paths
changed file set
gate report identities
review report identity
final evidence manifest identity
block/cancel reason
```

All state writes must use write-temp, fsync where appropriate, and atomic replace. Use an advisory lock per task to prevent two orchestrator processes mutating one task simultaneously.

## Worktree lifecycle

The orchestrator must never let an implementation agent edit the operator's primary Friday checkout.

### Prepare

```text
verify repository identity
verify requested base ref exists
resolve and record exact base commit
refuse a dirty primary checkout unless the operator explicitly selects a safe override
create branch `agent/<task-id>`
create worktree under the friday-dev state directory
verify worktree root and branch
render task-local OpenCode config outside the product repository
```

Recommended state layout:

```text
~/.local/share/friday-dev/
    tasks/<task-id>/
        task.toml
        state.json
        prompt/
        opencode/
            config.json
            agents/
            tools/
        runs/<run-id>/
            raw-events.jsonl
            stdout.log
            stderr.log
            result.json
        gates/
        reviews/
        evidence/
        HANDOFF.md

    worktrees/<repo-id>/<task-id>/
```

### Finalize

Before declaring `READY_FOR_HUMAN`:

```text
verify worktree and branch identity
compute diff from exact base commit
reject changed paths outside write_scope
reject forbidden paths regardless of write_scope ordering
record untracked files
run required deterministic gates
require a read-only review report
record unresolved findings
verify no remote ref mutation occurred
produce evidence manifest and handoff
```

### Cleanup

Cleanup must be explicit and confirmation-gated. It may remove the worktree only after state/evidence are durable. It must never delete the task branch or artifacts silently.

## OpenCode integration boundary

### Exact version pin

The first `friday-dev` release must support exactly one tested OpenCode version.

`friday-dev doctor` must report:

```text
expected version
observed version
compatible / unsupported / untested
config schema smoke result
JSON-event smoke result
```

An untested version may be allowed only through an explicit operator override and must be recorded in evidence. Do not silently follow OpenCode auto-update.

Generated environments should set at least the pinned-version equivalents of:

```text
OPENCODE_CONFIG=<task config path>
OPENCODE_CONFIG_DIR=<task OpenCode config directory>
OPENCODE_DISABLE_AUTOUPDATE=1
OPENCODE_AUTO_SHARE=false
OPENCODE_DISABLE_MODELS_FETCH=1
OPENCODE_DISABLE_CLAUDE_CODE=1
OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1
OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1
OPENCODE_CLIENT=friday-dev
```

Review whether default plugins and external global configuration can widen the task. The generated execution must be reproducible and isolated from unrelated user-wide OpenCode plugins, agents, commands and credentials. Prefer an explicit task config directory and only the orchestrator's shipped agents/tools.

### CLI backend

Define an internal protocol such as:

```python
class AgentBackend(Protocol):
    def check_version(self) -> BackendVersion: ...
    def run(self, request: AgentRunRequest) -> AgentRunResult: ...
    def continue_session(self, request: AgentContinueRequest) -> AgentRunResult: ...
    def export_session(self, session_id: str, *, sanitize: bool) -> Path: ...
```

The production v1 implementation invokes OpenCode with argv arrays, never a shell string.

Headless runs should resemble:

```text
opencode run
    --format json
    --dir <worktree>
    --agent <role>
    --model <provider/model>
    --title friday-dev:<task-id>:<phase>:<attempt>
    [--variant <variant>]
    <assembled prompt>
```

Continuation uses the recorded session ID. Do not identify sessions by “latest” when a concrete ID is available.

The event parser must be tolerant:

- preserve every raw JSON line;
- extract only a small stable envelope where possible;
- retain unknown events rather than failing or discarding them;
- stamp the parser and OpenCode version;
- never treat model prose saying “tests passed” as a gate result.

### Optional interactive continuation

After a headless session exists, `friday-dev task tui <task-id> --role implementer` may exec the OpenCode TUI with the exact recorded session and task-local environment.

Interactive mode does not weaken permissions, write scope, final diff validation, or human finalize requirements.

The first release does not need to manage `opencode serve`. Add a server backend only if CLI limitations are measured and documented.

## OpenCode roles

The first release uses three explicit roles. They may all use the same local model profile.

### Architect

Purpose:

```text
inspect current code and contracts
produce a bounded implementation plan
identify exact affected surfaces, risks, gates and rollback
make no file changes
```

Permissions:

```text
read/glob/grep/list/lsp: allow inside worktree
edit: deny
bash: deny
external_directory: deny
websearch/webfetch: deny by default
task/subagents: deny in v1
```

Output is captured by the orchestrator as `plan.md`. The model does not need file-write permission to save it.

### Implementer

Purpose:

```text
execute the approved bounded plan
edit only the declared worktree scope
use safe project actions for tests and inspection
stop with an honest partial result when blocked
```

Permissions:

```text
read/glob/grep/list/lsp: allow inside worktree
edit: allow inside worktree subject to final independent scope validation
built-in bash: deny
external_directory: deny
websearch/webfetch: deny by default
task/subagents: deny in v1
custom friday_exec: allow
```

Set bounded `steps` from TaskSpec. Reaching the bound produces a partial handoff, never an automatic success.

### Reviewer

Purpose:

```text
receive the original task contract, approved plan, exact diff and deterministic gate reports
look for regressions, scope creep, weakened tests, false completion and missing evidence
make no edits
```

Use a fresh OpenCode session. Do not give the reviewer the implementer's self-justification as primary evidence. The reviewer may receive raw task facts, diff, selected source files and gate reports.

Permissions are read-only. The reviewer cannot approve merge or release; it emits findings with severity and disposition.

## Restricted project execution tool

Deny OpenCode's built-in arbitrary `bash` tool for the implementer.

Provide one custom tool, tentatively named `friday_exec`, defined in TypeScript for OpenCode but delegated to the Python orchestrator command layer.

Suggested schema:

```text
action:
    git_status
    git_diff
    git_log
    pytest
    ruff_check
    ruff_format_check
    mypy
    compileall
    quality_gate
    inspect_file_status

args:
    structured fields specific to the action
```

Do not expose one unrestricted shell command string.

The Python executor must:

```text
resolve cwd to the exact task worktree
validate action and every path argument
use argv arrays without shell=True
strip the environment to an explicit allowlist
remove production, SSH, cloud, Telegram and user-data credentials
apply process timeout and output-byte caps
stream or spool full output to a task artifact
return a bounded structured result to the model
record executable, argv digest, cwd identity, exit code, duration and truncation state
```

For `pytest`, accept validated node IDs or worktree-relative paths. For `quality_gate`, accept only named profiles, not arbitrary extra arguments.

The deterministic orchestrator gate runner remains separate from model-invoked `friday_exec`. A tool result can inform the model, but only the orchestrator's final gate phase can satisfy TaskSpec completion.

## Environment isolation

Construct a minimal environment rather than inheriting the operator shell wholesale.

Allowed categories may include:

```text
PATH with known toolchain locations
HOME redirected to task-local temporary home where practical
TMPDIR under task state
locale and timezone
Python virtual environment variables
explicit local model endpoint variables
OpenCode task-local variables
```

Strip at least:

```text
SSH_AUTH_SOCK
GitHub/GitLab tokens
cloud credentials
Telegram tokens
production API keys
Friday live database paths
backup credentials
remote deployment variables
unrelated provider API keys
```

The endpoint API key, if any, is passed only to OpenCode through the named environment variable and never echoed into logs or manifests.

This boundary protects against accidental access. It is not represented as a hostile-code sandbox. If a stronger filesystem/network sandbox is later required, add it behind an `ExecutionSandbox` interface after the basic journey is stable.

## Prompt bundle

Do not ask the model to reconstruct the task from chat history alone.

Every phase prompt should be assembled deterministically from:

```text
task ID and immutable objective
resolved base commit
role and phase
approved plan when applicable
read/write/forbidden scope
required gates
explicit non-goals
canonical Friday status paths
previous attempt summary and unresolved findings
completion and stopping rules
```

Do not paste the whole repository or huge status documents automatically. Provide file references and let OpenCode inspect authorized sources.

The implementer prompt must explicitly require:

```text
no production access
no push/merge/deploy
no weakening of tests to fit broken behavior
no unsupported completion claim
no edits outside scope
honest partial/blocking state
concise final changed-files and remaining-work summary
```

## Deterministic gates

Gate definitions are code/config owned, not model authored.

The Friday repo profile should offer at least:

```text
focused
static
full_non_ui
custom_task_tests
```

The architect must map these to the current real Friday commands after inspecting `tools/quality_gate.py`, release tooling and current operator practice.

Each gate report records:

```text
gate ID and profile version
exact base and worktree commit/diff identity
argv and cwd
sanitized environment identity
start/end/duration
exit code
timeout or cancellation
stdout/stderr artifact paths and hashes
parsed pass/fail/skip counts where trustworthy
truncation state
final PASS / FAIL / ERROR / NOT_RUN
```

A skipped phase is not passed. A model statement is not a gate. A focused gate does not imply the full gate.

## Evidence manifest

Produce one machine-readable `evidence.json` and one human `HANDOFF.md` per task.

Minimum manifest fields:

```text
schema version
task ID and TaskSpec hash
friday-dev version and source commit
OpenCode version
model profile name, provider ID, endpoint origin hash and upstream model ID
base repository path hash, base ref and base commit
worktree branch and final tree/diff hash
phase/run/session identities
changed, added, deleted and untracked paths
scope validation result
architect plan artifact hash and approval event
implementer result state
review findings artifact hash and unresolved severity counts
gate reports and exact states
known skips and reasons
final orchestrator state
human finalization identity/time when performed
```

Do not store:

```text
API keys
raw environment values
live data paths
user document bodies
full source files
private model reasoning
```

Raw OpenCode events and transcripts remain local task artifacts with restrictive filesystem permissions. The evidence manifest references hashes and paths; it does not duplicate their content.

## CLI surface

The first release should remain small and composable.

Recommended commands:

```text
friday-dev init
friday-dev doctor [--model-profile NAME] [--repo-profile NAME]
friday-dev config show
friday-dev task create --spec FILE
friday-dev task prepare TASK_ID
friday-dev task plan TASK_ID
friday-dev task approve-plan TASK_ID [--plan FILE]
friday-dev task implement TASK_ID
friday-dev task continue TASK_ID --role ROLE --message TEXT
friday-dev task tui TASK_ID --role ROLE
friday-dev task gate TASK_ID [--profile NAME]
friday-dev task review TASK_ID
friday-dev task status [TASK_ID]
friday-dev task finalize TASK_ID
friday-dev task cancel TASK_ID --reason TEXT
friday-dev task cleanup TASK_ID
friday-dev task export TASK_ID [--sanitize]
```

Commands must have stable documented exit codes, including separate codes for:

```text
usage/configuration error
unsupported OpenCode version
endpoint unavailable
policy violation
agent process failure
gate failure
review blocking finding
human approval required
internal orchestrator error
```

Do not hide a failed phase behind exit code zero merely because a handoff was generated.

## Endpoint-independent implementation and tests

The complete initial tool must be buildable and testable before any LLM endpoint exists.

### Fake OpenCode backend

Provide a fake executable or backend adapter fixture that:

```text
accepts the expected argv
generates deterministic JSONL events
returns a fixed session ID
can simulate edits in a temporary git worktree
can simulate timeout, malformed event, unknown event and non-zero exit
can simulate continuation
```

Do not require a fake OpenAI wire server for the core test suite. That would couple tests to OpenCode's current provider protocol and create avoidable maintenance.

### Full fake golden journey

One test must prove:

```text
create temporary git repository
    -> create TaskSpec
    -> prepare isolated worktree
    -> run fake architect
    -> approve plan
    -> run fake implementer producing a controlled edit
    -> run deterministic test gate
    -> run fake read-only reviewer
    -> validate diff scope
    -> generate evidence and handoff
    -> finalize without modifying primary checkout
```

Adversarial tests must cover:

```text
dirty primary checkout
base ref moves after task preparation
path traversal
symlink escape
forbidden file edit
untracked forbidden file
attempted git push/systemctl/ssh action
secret environment stripping
concurrent task mutation
malformed OpenCode JSON
unknown OpenCode event
timeout and cancellation
gate output truncation
gate skip vs pass
review blocking finding
cleanup before durable evidence
```

## Connection phase when the endpoint exists

The operator should need to provide only endpoint facts, not modify source code.

### Required operator inputs

```text
FRIDAY_DEV_LLM_BASE_URL
FRIDAY_DEV_LLM_MODEL_ID
FRIDAY_DEV_LLM_API_KEY if authentication is enabled
context token limit
maximum output token limit
wire API: chat_completions or responses
optional model variant/reasoning setting
```

### `doctor` endpoint checks

Add opt-in live checks:

1. Resolve the configured profile without exposing secrets.
2. Verify endpoint reachability and exact model ID discovery where supported.
3. Run a short plain text completion.
4. Run a streaming completion and clean cancellation.
5. Run one harmless function/tool-call canary with a strict schema.
6. Confirm tool arguments arrive as valid structured data, not prose.
7. Confirm OpenCode can run one read-only headless session through the generated config.
8. Confirm the model can call `read` or `grep` in a tiny fixture repository and stop within bounded steps.
9. Record the endpoint software/model identity exposed by the server, when available.
10. Label any unsupported probe honestly rather than treating it as passed.

Do not run these probes against the Friday production repository first.

### First live canary sequence

Use a disposable fixture repository, then a clean Friday worktree.

#### Canary A: read-only repository explanation

```text
architect role only
no edits
identify one function and its tests
produce a bounded plan
```

#### Canary B: trivial controlled edit

```text
implementer edits one fixture/document file
runs one tiny deterministic test
scope validator confirms exactly one expected path
```

#### Canary C: real Friday read-only audit

```text
read current canonical status and one affected subsystem
produce an architecture audit
no edits
human compares usefulness with an external-model audit
```

#### Canary D: small real Friday patch

Choose a low-risk, reversible, independently testable task. Require:

```text
approved plan
small write scope
focused tests
fresh reviewer session
manual final acceptance
no production release
```

Only after these canaries should the tool be allowed on schema, release, security, production-routing or large refactor tasks.

## Ordered implementation packages

Implement as separate clean checkpoints. Do not deliver one giant “agent platform” commit.

### Package 0: architecture confirmation and standalone skeleton

Deliver:

- separate `friday-dev` git repository;
- architecture decision confirming OpenCode CLI backend, no fork and no own agent loop;
- package skeleton, formatting, typing and tests;
- pinned OpenCode version decision;
- operator-facing README with the endpoint-later promise.

Acceptance:

- no Friday runtime import;
- no LLM endpoint required;
- no daemon, database or web UI;
- runtime dependency budget is documented;
- installation and `friday-dev --help` work in a clean environment.

### Package 1: closed config, task and state contracts

Deliver:

- `GlobalConfig`, `ModelProfile`, `RepoProfile`, `TaskSpec`, `TaskState`;
- atomic task store and locks;
- environment-variable substitution with secret-safe rendering;
- stable exit codes and errors.

Acceptance:

- unknown fields fail or are handled by explicit versioned extension points;
- secrets never appear in serialized state;
- malformed scopes and unsafe task IDs fail closed;
- concurrent mutation is covered.

### Package 2: repository and worktree lifecycle

Deliver:

- repository identity checks;
- exact base commit resolution;
- worktree create/inspect/finalize/cleanup;
- diff and untracked path scope validator;
- symlink/path traversal protection.

Acceptance:

- primary checkout is never edited by the fake journey;
- forbidden-path edits block finalization;
- base movement after preparation is visible and cannot silently change task identity;
- cleanup cannot precede durable evidence without explicit force and warning.

### Package 3: pinned OpenCode compatibility and generated roles

Deliver:

- `AgentBackend` protocol;
- CLI backend and fake backend;
- exact OpenCode version check;
- task-local config renderer;
- architect, implementer and reviewer agents;
- bounded `steps` and explicit permissions;
- reproducible isolated OpenCode environment.

Acceptance:

- no global/user plugin or credential accidentally widens the task;
- architect and reviewer cannot edit;
- implementer cannot use built-in bash or external directories;
- raw JSON events are retained and unknown events survive parsing;
- unsupported OpenCode versions fail with a useful diagnostic.

### Package 4: restricted `friday_exec`

Deliver:

- TypeScript OpenCode tool definition;
- Python named-action executor;
- argv/path/env validation;
- timeouts, cancellation and output caps;
- structured receipts.

Acceptance:

- no raw shell string path exists;
- attempts to run push, SSH, systemctl, Docker, curl/wget or arbitrary commands fail closed;
- tests and selected quality actions work in temporary repositories;
- full output is spooled while model-visible output is bounded and labelled.

### Package 5: phase runner and human approval boundaries

Deliver:

- plan, approve-plan, implement, continue and review commands;
- separate sessions per role;
- attempt budgets and phase state machine;
- prompt bundle construction;
- optional TUI resume of an existing session.

Acceptance:

- implementation cannot begin before required plan approval;
- reviewer receives task/diff/gates rather than implementer persuasion;
- a timeout or step limit produces partial/blocked state;
- repeated implementation attempts are individually recorded;
- no phase automatically merges, pushes or deploys.

### Package 6: gates, evidence and handoff

Deliver:

- Friday gate profiles based on current real tooling;
- deterministic gate reports;
- scope validation;
- `evidence.json` and `HANDOFF.md`;
- final human-ready state.

Acceptance:

- model prose cannot satisfy a gate;
- skip/error/fail/pass remain distinct;
- manifest hashes reconcile to artifacts;
- finalization blocks on forbidden paths, required gate failure or blocking review findings;
- a human may explicitly accept a documented non-blocking exception, which is recorded rather than erased.

### Package 7: complete fake golden journey and operator runbook

Deliver:

- full endpoint-free journey test;
- adversarial matrix;
- install/run/backup/cleanup documentation;
- example global config and TaskSpec;
- exact instructions for later endpoint connection.

Acceptance:

- CI runs with no LLM and no network dependency;
- the fake journey proves worktree, phase, gate, review and evidence flow;
- docs do not imply a real model has been certified.

### Package 8: endpoint integration canary, deferred until operator endpoint exists

Do not block Packages 0-7 on this package.

When the operator supplies endpoint facts:

- render the profile;
- run `doctor` live probes;
- execute Canaries A-D in order;
- record latency, tool-call reliability, malformed calls, step usage and intervention count;
- tune only measured profile settings;
- freeze the first locally certified model/profile identity.

No production release work is performed as part of model certification.

## Example operator flow after delivery

The exact syntax may differ slightly after CLI review, but the workflow should remain this simple.

```bash
# One-time configuration
friday-dev init
export FRIDAY_DEV_LLM_BASE_URL="http://127.0.0.1:8000/v1"
export FRIDAY_DEV_LLM_MODEL_ID="glm-5.2"
# export FRIDAY_DEV_LLM_API_KEY="..."  # only when required

# Verify toolchain and model endpoint
friday-dev doctor --model-profile local-glm --repo-profile friday

# Create a task from a prepared spec
friday-dev task create --spec tasks/source-ref-foundation.toml
friday-dev task prepare 2026-08-23-source-ref-001

# Read-only plan
friday-dev task plan 2026-08-23-source-ref-001
friday-dev task approve-plan 2026-08-23-source-ref-001

# Implementation and deterministic checks
friday-dev task implement 2026-08-23-source-ref-001
friday-dev task gate 2026-08-23-source-ref-001 --profile focused
friday-dev task review 2026-08-23-source-ref-001

# Produce evidence for human inspection
friday-dev task finalize 2026-08-23-source-ref-001
friday-dev task status 2026-08-23-source-ref-001
```

The final output points to:

```text
worktree
exact diff
plan
agent run artifacts
gate reports
review findings
evidence.json
HANDOFF.md
```

The operator then uses the existing Friday architect/release process to accept, revise, merge or discard the work.

## Initial model usage policy

One GLM-5.2-class profile is sufficient for the first release.

Recommended role behavior:

```text
architect:
    same model, read-only, lower temperature, 20-30 steps

implementer:
    same model, edit + friday_exec, 30-60 steps depending on task

reviewer:
    same model, new read-only session, low temperature, 20-30 steps
```

Do not assume huge context should be filled. Start with measured practical limits, likely 64K-128K, and let OpenCode retrieve files. Increase only when Friday-specific evaluations show a benefit.

The profile must preserve endpoint-declared context/output limits honestly. Do not configure a 1M context merely because the base model family advertises it if the deployed quantization/runtime does not support it reliably.

## Evaluation after the model is connected

Create a small Friday Development Benchmark from real, de-identified historical tasks.

Candidate categories:

```text
read-only architectural audit
small regression fix
schema migration foundation
rollback-safe change
typed outcome adapter
fixture repair without weakening production behavior
small module extraction
status/evidence update
review of another patch
```

Measure:

```text
task completion
first-pass focused gate
first-pass full applicable gate
forbidden-scope violations
unnecessary changed files
weakened tests
false completion claims
malformed tool calls
human interventions
agent steps
time to first useful action
total wall time
reviewer defect catch rate
handoff quality
```

External GPT/Grok/Claude reviews may remain a sparse independent calibration set rather than the default implementation path.

## Explicit non-goals

The architect must resist these expansions during v1:

- no OpenCode fork;
- no generic multi-provider router beyond simple named profiles;
- no autonomous issue queue;
- no automatic branch merge, push, PR, release or deployment;
- no production SSH/systemd access;
- no arbitrary shell;
- no browser or web research tools by default;
- no MCP expansion;
- no model fine-tuning pipeline;
- no vector database or repo RAG service;
- no attempt to replace Friday's architect, canonical status or release operator;
- no shared multi-user service;
- no Docker requirement;
- no work on Friday's active product roadmap counted as part of this tool.

Add a feature only after a real failed development journey demonstrates the need.

## Required architect deliverables

The architect should return:

1. A current OpenCode compatibility note naming the exact pinned version and tested CLI/config behavior.
2. A standalone `friday-dev` repository with clean history and the package sequence above.
3. A concise architecture document and security boundary.
4. Endpoint-free unit, integration and full fake-journey tests.
5. A shipped Friday repository profile based on current real gates and protected paths.
6. Example global/model config with empty endpoint placeholders.
7. Example TaskSpecs for read-only audit and bounded implementation.
8. Operator runbook containing the exact endpoint variables and `doctor` sequence.
9. Evidence from the fake golden journey.
10. A deferred live-canary checklist that requires no source-code changes when the endpoint arrives.
11. A final handoff stating source commit, tested OpenCode version, tests, known limitations and the exact first operator command.

Do not change Friday production, product schema, canonical roadmap, or active release state merely to implement this separate tool.

## Definition of done before the endpoint arrives

Packages 0-7 are complete when:

1. `friday-dev` installs in a clean environment.
2. The exact OpenCode version is pinned and compatibility-checked.
3. No real model endpoint is required for CI.
4. A fake full journey creates and edits only an isolated worktree.
5. Plan approval is required before implementation.
6. Built-in arbitrary bash and external directories are denied.
7. The restricted executor runs approved tests and blocks dangerous actions.
8. Diff scope, untracked files and forbidden paths are independently validated.
9. Gate states cannot be invented by the model.
10. Review uses a fresh read-only session contract.
11. Evidence and handoff reconcile to the exact task, base commit, diff and gate artifacts.
12. No command can push, merge, deploy or touch production.
13. The operator can connect a future endpoint by changing configuration/environment only.

## Definition of first local certification after the endpoint arrives

The local model/profile becomes certified for bounded Friday development only when:

1. `doctor` plain, streaming and tool-call probes pass.
2. OpenCode config/model discovery matches the intended endpoint and model identity.
3. Read-only fixture and Friday audits behave correctly.
4. A controlled edit remains within scope and passes its deterministic test.
5. One low-risk real Friday patch completes plan, implementation, gate, review and evidence phases.
6. No production, live data, push, merge or deployment access is used.
7. Observed limits, malformed calls, interventions and latency are recorded.
8. The operator explicitly accepts the profile for a named class of tasks.

Certification applies to the exact combination of:

```text
model weights and quantization
inference runtime and version
chat/tool parser configuration
context/output limits
OpenCode version
friday-dev version
role profile
```

Changing a material component requires a bounded re-canary.

## Final direction

The requested system is deliberately modest:

```text
one local coding model
one mature generic coding-agent engine
one small project-specific control plane
one isolated worktree per task
one deterministic evidence trail
one human at the merge/release boundary
```

That is enough to reduce routine dependence on external coding providers without creating a second Friday-sized project.

Build the rails now with a fake backend. When the operator raises GLM or another OpenAI-compatible endpoint, connection should be configuration work followed by a measured canary, not another architecture project.
