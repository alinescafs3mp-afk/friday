# Friday Host Capability Plane: Application Installation, Execution, and Functional Control

> Document ID: FRIDAY-HOST-CONTROL-001  
> Status: Architect implementation instruction  
> Date: 25 August 2026  
> Observed repository checkpoint: `main` at `fb8de793a47ec50fe8cef60dce72b729fd05e94e`  
> Observed product version: Friday `0.207.27`  
> Audience: Friday architect and implementation lead  
> Primary platform: the Ubuntu workstation that hosts Friday  
> Required first vertical slice: install `nmap` when absent, scan an authorized local subnet, parse the result, and report evidence

## Read this first

The requested feature is not an application launcher.

Friday must be able to:

1. discover software already installed on the Ubuntu host;
2. understand which useful actions that software can perform;
3. execute those actions rather than merely open a window;
4. install missing software after an exact, human-authorized package plan;
5. resume the original task after installation without making the user repeat it;
6. collect structured output, files, process state, and other evidence;
7. explain what actually happened and distinguish success, partial success, failure, cancellation, and unknown outcome;
8. preserve Friday's existing permission, approval, idempotency, audit, evidence, and fail-closed contracts.

A representative user request is:

```text
Install nmap if necessary, scan 192.168.1.0/24, and tell me what is alive and which services are exposed.
```

The successful product journey is:

```text
natural-language request
    -> capability resolution
    -> detect that nmap is absent
    -> exact package plan
    -> owner approval
    -> bounded package transaction
    -> post-install attestation
    -> resume original work item
    -> bounded nmap action
    -> XML parse and coverage assessment
    -> evidence-backed answer and durable receipt
```

Do not implement only `app_launch`. Do not expose a root shell, `sudo`, arbitrary `bash -c`, the Docker socket, or the current `code_run` as a substitute. Those shortcuts would turn a useful hand into an unlabelled box of detonators.

## Relationship to existing repository work

Before implementation, re-read the current:

- [`README.md`](../README.md);
- [`docs/SECURITY.md`](../docs/SECURITY.md);
- [`friday/agent_runtime/__init__.py`](../friday/agent_runtime/__init__.py);
- [`friday/execution_kernel/__init__.py`](../friday/execution_kernel/__init__.py);
- [`friday/permissions/__init__.py`](../friday/permissions/__init__.py);
- [`friday/config/__init__.py`](../friday/config/__init__.py);
- [`docker-compose.yml`](../docker-compose.yml);
- effect outcome, approval, failure, and Work Item contracts under [`friday/orchestration/`](../friday/orchestration/) and [`friday/interaction_control_plane/`](../friday/interaction_control_plane/);
- [`MCP_ARCHITECTURE_OBSERVATION.md`](MCP_ARCHITECTURE_OBSERVATION.md);
- [`ENGINEER_MODE_REVERSE_ENGINEERING_AND_HOST_ASSESSMENT_ARCHITECT_BRIEF.md`](ENGINEER_MODE_REVERSE_ENGINEERING_AND_HOST_ASSESSMENT_ARCHITECT_BRIEF.md).

Important current facts:

- `code_run` executes isolated Python inside the backend environment. It is disabled by default, high-risk, and explicitly not an OS sandbox.
- The normal Docker backend cannot see arbitrary host executables, the user's Wayland session, the user D-Bus, or host package management.
- The backend is read-only, drops capabilities, uses `no-new-privileges`, and intentionally has no Docker socket.
- `host.docker.internal` already resolves to the host gateway, but a network listener should not be the default host-control transport.
- Friday already owns actor authorization, per-tool capability checks, high-risk approvals, effect receipts, audit, durable continuation, generated-file delivery, and honest failure behavior.
- The optional secondary brain has no authority to invoke tools, create effects, or publish. Preserve that boundary.

This brief adds one native edge subsystem. It does not replace Agent Runtime, create a second orchestrator, or redesign Friday around MCP.

## Product contract

### What the user should be able to ask

Examples that must fit the same architecture:

```text
Scan my local subnet with nmap and summarize the devices.
Convert these videos to H.265 with ffmpeg.
Open VLC and play the latest recording.
Use LibreOffice to convert these DOCX files to PDF.
Install jq and extract these fields from the JSON files.
Show me which Friday-launched applications are still running.
Cancel the long conversion job.
Install a suitable tool for reading this file format and use it.
```

The model must not need a hard-coded tool for every executable. The system needs a typed capability catalog, trusted built-in adapters, and a controlled route for onboarding new command-line applications.

### What “use an application” means

Launching a process is only one possible action. Functional control can use, in preferred order:

1. a stable command-line interface with structured output;
2. a local application API;
3. a D-Bus interface;
4. a documented automation interface such as UNO or MPRIS;
5. an accessibility tree for a specifically identified application window;
6. visual interaction only as an optional last resort.

Prefer semantic APIs over screen clicking. A CLI returning XML is a tool. A mouse cursor guessing where a button moved is weather.

### Non-goals for the first implementation

Do not make the first release depend on:

- universal control of every arbitrary GUI application;
- a root shell or general-purpose remote administration agent;
- unattended package installation;
- adding package repositories, PPAs, signing keys, or `curl | sh` installers;
- global `pip`, `npm`, or random binary installation;
- arbitrary NSE script execution;
- exploit validation, lateral movement, persistence, credential spraying, or denial of service;
- replacing the existing browser and web contours with control of the user's personal browser session;
- making the host agent mandatory for normal Friday operation.

The architecture must permit later expansion without pretending these hard problems are already solved.

## Governing invariants

These invariants are required, not suggestions.

1. **Friday Core owns policy.** The model proposes intent. Code selects the adapter, validates arguments, authorizes the actor, computes risk, creates the exact plan, and decides whether approval is required.
2. **The host agent owns execution.** It resolves the final executable, verifies provenance and version, creates the process, captures output, and returns a signed receipt.
3. **The privileged broker owns package mutation only.** It does not run arbitrary commands and does not accept model-authored shell text.
4. **No shell interpolation.** Every program starts with an executable reference and an `argv` array. No `bash -c`, `sh -c`, command substitution, pipes, redirects, globs, or environment-controlled executable lookup.
5. **Installation is not authorization to use everything.** A newly installed program becomes available only through a trusted adapter or an explicitly approved one-shot execution plan.
6. **Approval binds exact bytes and arguments.** If package versions, dependencies, executable identity, targets, paths, or arguments drift after approval, execution stops and a new plan is required.
7. **Unknown outcome is durable.** A lost connection after a possible effect never causes an automatic retry. Reconcile first.
8. **No silent privilege.** Friday never stores or asks the model to handle a sudo password. Initial broker installation may require the operator to run one local setup command.
9. **No unbounded target expansion.** Network actions remain inside the exact target set and code-owned CIDR policy.
10. **Tool output is untrusted data.** Stdout, stderr, XML, JSON, accessibility text, and application responses cannot create tool calls or alter control flow.
11. **Feature absence is fail-soft.** When host control is disabled, disconnected, or unsupported, Friday continues normal dialogue and knowledge work.
12. **Claims require postconditions.** Friday says “installed,” “converted,” “scanned,” “opened,” or “stopped” only when the relevant receipt and postcondition support that statement.

## Required architecture

Use three trust domains.

```text
┌───────────────────────────────────────────────────────────────────────┐
│ Friday backend in Docker                                             │
│                                                                       │
│ intent routing -> capability catalog -> permission -> exact plan      │
│ -> approval -> durable job/effect -> signed host request              │
│                                                                       │
│ No host shell, no sudo password, no Docker socket                     │
└──────────────────────────────┬────────────────────────────────────────┘
                               │ versioned authenticated IPC
                               │ preferred: bind-mounted Unix socket
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│ friday-host-agent, systemd user service                              │
│                                                                       │
│ inventory, adapter validation, CLI/API/D-Bus execution, GUI session   │
│ integration, systemd transient jobs, output capture, receipts         │
│                                                                       │
│ Runs as the desktop user, never as root                               │
└──────────────────────────────┬────────────────────────────────────────┘
                               │ narrow system D-Bus or root-owned UDS
                               │ exact package transaction only
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│ friday-package-broker, systemd system service                         │
│                                                                       │
│ resolve/plan/install/remove via approved package managers             │
│ no generic executable API, no shell, no repository modification       │
└───────────────────────────────────────────────────────────────────────┘
```

### Why two host-side processes

The desktop user service needs the user session, user files, D-Bus, Wayland metadata, and ordinary application execution. It must not run as root.

Package installation needs privilege and can execute package maintainer scripts as root. That surface must be tiny, separately audited, and incapable of becoming a command proxy.

Do not merge both into one root daemon merely because it is convenient.

## Suggested code layout

The architect may adjust names, but preserve the boundaries.

```text
friday/host_control/
    __init__.py
    client.py                 backend IPC client
    contracts.py              versioned request/result models
    capability_catalog.py     bounded discovery for Agent Runtime
    policy.py                 code-owned admission and risk policy
    plans.py                  exact immutable action plans
    jobs.py                   durable job integration
    receipts.py               evidence and postcondition validation
    tools.py                  stable Friday tool definitions
    work_items.py             continuation across install and execution
    result_projection.py      bounded model-facing output
    adapters/
        __init__.py
        base.py
        nmap.py
        ffmpeg.py
        libreoffice.py
        jq.py
        mpris.py

friday_host_agent/
    __init__.py
    daemon.py
    protocol.py
    authentication.py
    inventory.py
    executable_attestation.py
    adapter_registry.py
    process_runner.py
    systemd_jobs.py
    file_grants.py
    dbus_control.py
    desktop_control.py
    receipts.py
    package_client.py
    adapters/

friday_package_broker/
    __init__.py
    daemon.py
    protocol.py
    apt_backend.py
    transaction.py
    policy.py
    receipts.py

deploy/host-control/
    README.md
    install.sh
    uninstall.sh
    compose.override.yml
    systemd/user/friday-host-agent.service
    systemd/system/friday-package-broker.service
    systemd/system/friday-package-broker.socket
    polkit/io.friday.package-broker.policy
    examples/policy.toml
```

Keep host-control code out of the already enormous execution-kernel module except for narrow registration seams. Do not add another several-thousand-line island to `friday/execution_kernel/__init__.py`.

## Backend-facing tools

Expose a small stable set of code-owned tools. Do not dump every `.desktop` file or application `--help` page into the model context.

Recommended tool surface:

```text
host_capability_search
host_capability_describe
host_action_run
host_job_status
host_job_cancel
software_search
software_install
software_remove
```

Optional owner-only escape hatch, disabled by default:

```text
host_program_run_once
```

### `host_capability_search`

Inputs:

```json
{
  "query": "scan the local network",
  "category": "network"
}
```

Returns only a bounded set of installed or installable capabilities:

```json
{
  "capabilities": [
    {
      "capability_id": "network.nmap.scan",
      "state": "installable",
      "summary": "Discover hosts and inspect services with bounded nmap profiles",
      "actions": ["discover", "services", "selected_ports"],
      "package_candidate_ref": "opaque-signed-reference"
    }
  ]
}
```

The model receives capability semantics, not executable paths and not raw package-manager output.

### `host_action_run`

Inputs are semantic:

```json
{
  "capability_id": "network.nmap.scan",
  "action": "discover",
  "arguments": {
    "targets": ["192.168.1.0/24"],
    "profile": "lan_discovery"
  }
}
```

Friday Core must:

1. resolve the exact adapter version;
2. validate arguments against the adapter schema;
3. normalize addresses, paths, enums, sizes, and timeouts;
4. authorize the effective capability;
5. create an immutable `HostActionPlan`;
6. apply approval policy;
7. create an idempotent durable job;
8. send only the signed exact plan to the host agent.

The model must not supply `argv`, executable paths, environment variables, systemd properties, parser class names, privilege profiles, or risk levels to this ordinary tool.

### `software_search`

Search local package metadata only. It does not mutate the host.

Return:

- package manager;
- exact package name;
- candidate version;
- configured origin and repository;
- architecture;
- installed state;
- summary;
- download size if known;
- dependency summary;
- whether the package has a trusted Friday adapter;
- an opaque candidate reference.

Do not search arbitrary internet download pages from this tool.

### `software_install`

Accept an opaque candidate or plan reference, not a free-form shell command.

The user-visible approval must show at minimum:

```text
Package manager
Exact requested package and version
Packages added, upgraded, downgraded, or removed
Configured repository origin
Download size
Estimated disk change
Services that may be added or restarted, when detectable
Friday capabilities expected to become available
The original task that will resume after installation
```

Package installation always requires a human approval bound to the exact plan digest.

### `host_program_run_once`

This is the broad escape hatch for a newly installed or unusual CLI before a reusable adapter exists. It is not the primary route.

Rules:

- owner-only by default;
- disabled by default;
- exact executable must come from the attested host inventory;
- arguments are an array, never a shell string;
- exact rendered command, working directory, files, network profile, timeout, and expected outputs are shown before approval;
- every invocation requires approval;
- no arbitrary environment variables;
- no stdin containing secrets unless delivered through a dedicated secret reference;
- no executable from a writable untrusted directory;
- no `sudo`, shells, terminal emulators, interpreters, package managers, or commands whose purpose is to execute another arbitrary program;
- successful one-shot actions may be proposed as adapter drafts, but are not silently promoted.

This route provides breadth without pretending that unrestricted shell access is a safe application API.

## Capability adapters

### Adapter identity

Each adapter must have a stable identity and a versioned contract:

```yaml
adapter_id: network.nmap
adapter_schema_version: 1
implementation_version: 1
supported_platforms: [ubuntu]
packages:
  apt:
    - name: nmap
executables:
  nmap:
    package_owner: nmap
    allowed_paths:
      - /usr/bin/nmap
actions:
  discover:
    security_id: host.network.scan
    risk_class: network_observe
    input_schema: nmap_discover_v1
    execution_profile: cli_network_unprivileged
    output_parser: nmap_xml_v1
    timeout_sec: 300
    max_output_bytes: 8388608
```

This example is descriptive. Trusted adapters should implement argument construction in reviewed code. Do not introduce a templating language powerful enough to smuggle shell syntax back into the system.

### Adapter states

At runtime an adapter is exactly one of:

```text
available
missing_package
unsupported_version
needs_setup
disabled
quarantined
unattested
```

Do not report a capability as available merely because a same-named executable exists somewhere in `PATH`.

### Executable attestation

Before every execution, record and verify:

- canonical absolute path;
- owning package where applicable;
- package version and architecture;
- file device and inode;
- file mode and owner;
- size and modification time;
- SHA-256 when the adapter or risk class requires it;
- adapter version;
- observed program version through a code-owned method, when safe.

If the executable changes between planning and execution, invalidate the plan.

Never resolve executables through a model-controlled `PATH`. The agent owns a minimal fixed search path and preferably uses absolute paths.

### Built-in first adapters

Implement enough diversity to prove the subsystem is general:

1. `network.nmap`
2. `media.ffmpeg`
3. `documents.libreoffice`
4. `data.jq`
5. `desktop.mpris`

The first release can ship only a subset operationally, but the architecture and tests must not be nmap-specific.

## Action execution profiles

Every action selects a code-owned execution profile.

Recommended profiles:

```text
cli_local_readonly
cli_workspace_transform
cli_network_unprivileged
gui_launch_user_session
dbus_application_control
accessibility_application_control
package_transaction
```

Each profile defines:

- permitted address families;
- filesystem grants;
- working directory;
- environment allowlist;
- CPU, memory, task, file-size, and wall-time budgets;
- output limits;
- whether a graphical session is required;
- whether network access is allowed;
- cancellation behavior;
- postconditions;
- risk and approval rules.

### Process creation

For CLI work, prefer a transient `systemd --user` unit or scope per job so that Friday can:

- assign a stable job unit name;
- set `RuntimeMaxSec`, `MemoryMax`, `TasksMax`, and CPU limits;
- stop the entire cgroup rather than one parent PID;
- query state after an agent restart;
- prevent orphaned descendants;
- collect exit status and signal information.

Use direct `execve` semantics. The process receives a minimal environment. Include only adapter-approved values such as locale, temporary directory, job workspace, and application-specific variables.

A GUI launch may require the user's session environment. Import and attest only the necessary values such as `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, and D-Bus address. Never copy Friday API tokens, LLM keys, Telegram credentials, or the backend environment into a launched application.

### Output capture

Store raw stdout and stderr as bounded evidence files outside the model prompt. Return a structured projection containing:

- exit code or signal;
- start and end times;
- timeout and truncation flags;
- parser status;
- bounded warnings;
- references and hashes for raw output;
- structured result;
- coverage and completeness state.

A parser failure does not convert successful process execution into a trustworthy semantic result. Report the layers separately.

## File and workspace authority

Do not give the host agent free path access to Friday's database or all user files.

Create a shared host-action workspace under the existing host data root, for example:

```text
${FRIDAY_HOST_HOME}/data/host-jobs/<job_id>/
    input/
    work/
    output/
    evidence/
    receipt/
```

The backend sees the same location under `/runtime/data/host-jobs/<job_id>/`.

The host agent receives opaque file grants or workspace-relative references. It must not accept arbitrary paths from the model.

For access outside the Friday data tree:

- require an operator-configured allowed root or an explicit per-job path grant;
- resolve paths without following an escaping symlink;
- recheck ownership and path identity immediately before use;
- distinguish read, create, replace, and delete grants;
- never overwrite an original input by default;
- return derived files through Friday's existing generated-file and delivery contour.

Where practical on Linux, use `openat2`-style beneath/no-symlink guarantees or an equivalent descriptor-based implementation rather than string-prefix checks.

## Software installation plane

### Package manager scope

The first supported package manager is Ubuntu APT using only repositories already configured and authenticated by the operating system.

Phase 2 may add:

- user-level Flatpak from explicitly configured remotes;
- dedicated `pipx` or `uv tool` environments under a Friday-owned unprivileged prefix;
- Snap through a separate reviewed backend.

Do not put all package managers behind one untyped command method.

### APT planning

Planning must be non-mutating and produce a canonical transaction proposal:

```text
requested package refs
resolved exact versions
new dependencies
upgrades
downgrades
removals
held-package conflicts
repository origins
architectures
download size
disk delta
configuration or reboot warnings
plan digest
expiry
```

The package plan may change while waiting for approval. Therefore execution must resolve again and compare the canonical plan. Any material drift invalidates the approval.

Pin exact versions during the approved transaction where APT permits it. Reject unauthenticated packages and repository-origin drift.

### Privileged package broker API

The broker surface should be approximately:

```text
Health
Resolve
PlanInstall
ExecuteInstall
PlanRemove
ExecuteRemove
TransactionStatus
CancelBeforeCommit
```

It must not expose:

```text
RunCommand
RunShell
AddRepository
ImportKey
InstallLocalDeb
RunPostInstallScript
SetArbitraryAptOption
WriteFile
StartService
```

The broker may use `python-apt`, PackageKit, or carefully constructed direct APT calls. The architectural requirement is the closed API and transaction validation, not one library choice.

### Package transaction receipt

Record:

- broker and package-manager versions;
- exact approved and executed plan digests;
- package state before and after;
- packages installed, upgraded, removed, or left unchanged;
- origins and versions;
- transaction timestamps;
- exit and lock state;
- bounded apt/dpkg evidence references;
- services or units observed as newly present, enabled, started, restarted, or failed;
- reboot-required state;
- post-install executable and adapter attestation;
- whether the original capability became available.

APT rollback is not guaranteed. Do not promise rollback merely because a list of changed packages exists. Removal or downgrade is a new separately approved transaction unless the host has an independently verified filesystem snapshot mechanism.

### Installation approval and continuation

Package installation always uses Friday's existing human approval contour.

The approval fingerprint includes at minimum:

```text
actor and own_id
host agent identity
package manager
requested package candidates
complete canonical transaction plan
repository origins
original user request
continuation work item
expiry
```

After successful installation, Friday must resume the original task from a durable Work Item. It must not ask the user to repeat “now scan the subnet.”

If installation succeeds but the adapter remains unavailable, report that precise state and do not improvise a shell command.

## Onboarding newly installed applications

A broad application system needs a route between “known built-in adapter” and “unrestricted shell.”

Implement three trust tiers.

### Tier 1: reviewed built-in adapter

Code and parser are part of the Friday repository. This is preferred for common or sensitive tools.

### Tier 2: operator-approved declarative adapter

Friday may draft a closed declarative adapter from package metadata, package-owned documentation, man pages, and explicitly requested inspection. The draft is inactive until:

1. the executable is package-attested;
2. the input schema is closed and bounded;
3. the argument mapping uses only literals, enums, bounded scalars, and file/target references;
4. the adapter declares filesystem and network policy;
5. the output parser is a built-in safe parser or bounded text projection;
6. a smoke test passes in a constrained job workspace;
7. the operator sees and approves the exact exposed actions;
8. the adapter is bound to package and executable versions.

Runtime-generated Python or shell is not an adapter format.

Version drift returns the adapter to `unattested` until revalidation.

### Tier 3: one-shot exact program execution

Use `host_program_run_once` only when the owner explicitly chooses it. A successful one-shot action can become evidence for a Tier 2 proposal, but never activates one automatically.

This three-tier model lets Friday grow new hands without replacing its skeleton with a command prompt.

## GUI and desktop application control

### Capability levels

Report desktop control capability honestly:

```text
no_graphical_session
launch_only
dbus_control
accessibility_control
visual_control
```

Do not claim generic GUI control merely because `DISPLAY` or `WAYLAND_DISPLAY` exists.

### Preferred interfaces

Examples:

- media players through MPRIS;
- LibreOffice through headless conversion or UNO;
- applications with documented local sockets or APIs through those APIs;
- desktop activation through `.desktop` entries or D-Bus activation;
- file opening through XDG portals where appropriate.

Do not automate the user's personal browser profile for ordinary web work. Continue using Friday's controlled web/browser contour.

### Accessibility control

If AT-SPI integration is added:

- bind actions to a specific PID, cgroup, desktop file, and window identity;
- retrieve only a bounded accessibility subtree;
- redact password roles and secret-like fields;
- reject interaction with authentication dialogs, password managers, wallets, terminals, package managers, system settings, and privilege prompts unless a dedicated reviewed adapter exists;
- use semantic roles and actions, not arbitrary coordinates;
- revalidate the element immediately before invoking it;
- record before/after accessibility evidence;
- require approval for destructive, financial, publishing, sending, deleting, or security-sensitive UI effects.

### Visual control

Wayland intentionally restricts global capture and synthetic input. Use portals and explicit session grants. Treat visual-only automation as optional and fragile. It must never be the hidden fallback when a semantic adapter fails.

### Friday-launched sessions

By default, Friday may control only application sessions it launched and tagged through a job/cgroup identity. Attaching to an already running user application requires explicit approval because its windows may contain unrelated private material.

## Network scanning with nmap

The nmap adapter is the first end-to-end acceptance target.

### Shared integration with engineer mode

Do not implement two nmap adapters.

The general host capability plane and `/engeneer` host assessment must share:

- target normalization;
- executable and tool-version attestation;
- code-owned argument construction;
- XML parsing;
- evidence format;
- target scope policy;
- timeout and output limits;
- result and coverage contracts.

They may select different executor profiles. Ordinary host control should use an unprivileged local network profile. Engineer mode may later use its isolated network worker for deeper explicitly authorized profiles. The adapter and parser remain one implementation.

This brief amends the earlier engineer brief only to forbid duplicated nmap integration. It does not broaden exploit validation into ordinary dialogue.

### Default target policy

Default allowed network targets are:

1. exact loopback targets when relevant;
2. directly connected private IPv4 networks;
3. directly connected IPv6 link-local or operator-approved ULA networks;
4. additional CIDRs explicitly configured by the owner.

Public network scanning is disabled by default. Enabling it requires an explicit operator policy and per-action approval.

The adapter must:

- parse CIDRs and addresses in code;
- cap target count;
- pin the exact normalized target set;
- prevent implicit neighbor or route expansion;
- resolve hostnames before execution and recheck every result against policy;
- reject command-shaped target strings;
- record interface and route evidence used to classify a target;
- refuse a scan if policy cannot prove the scope.

### Initial nmap actions

Implement closed profiles rather than arbitrary flags.

```text
discover
    bounded host discovery for an exact target set

services
    unprivileged TCP connect scan of a bounded port profile with light version detection

selected_ports
    exact user-selected TCP ports within configured count limits

full_tcp
    optional long scan, separately approved and disabled by default
```

Recommended ordinary unprivileged behavior:

- no shell;
- `-oX -` or a controlled XML output file;
- no model-supplied raw flags;
- bounded retries, host timeout, rate, and parallelism;
- `-sT` for service scanning;
- light service detection only where enabled;
- no arbitrary NSE scripts;
- no decoys, spoofing, fragmentation, evasion, source-port tricks, credential scripts, brute force, exploitation, or denial-of-service profiles.

Do not grant `CAP_NET_RAW`, `CAP_NET_ADMIN`, setuid, or root to the ordinary host agent merely to unlock SYN, OS, or broad UDP scanning. Deeper privileged scans belong in a separately reviewed execution profile, preferably the existing isolated engineer-network contour.

### nmap result contract

Parse XML into a stable result:

```text
targets_requested
targets_resolved
targets_scanned
targets_skipped
hosts_up
hosts_down_or_unknown
open_ports
closed_ports_when_requested
filtered_or_unknown_ports
service_guesses with confidence
scan_start and end
nmap version
exit status
warnings
coverage grade
raw XML evidence ref and hash
```

A timeout, truncated XML, parser error, privilege downgrade, skipped target, or unresolved hostname lowers coverage. Friday must not summarize a partial scan as complete.

### Required nmap journey

```text
User: Install nmap if needed and scan 192.168.1.0/24.

Friday Core:
- resolves network.nmap.scan;
- sees missing_package;
- creates a durable continuation work item;
- resolves the exact APT transaction;
- asks for approval with package/version/dependency/origin details.

After approval:
- package broker executes the exact transaction;
- host agent attests /usr/bin/nmap and its version;
- Friday resumes the original scan intent;
- target policy proves 192.168.1.0/24 is allowed;
- adapter runs a bounded profile;
- XML is parsed and raw evidence retained;
- Friday reports devices, services, warnings, and coverage.
```

No second request from the user is required after installation.

## Authorization and approval model

Add explicit capabilities under [`friday/permissions/__init__.py`](../friday/permissions/__init__.py), for example:

```text
host.capabilities.read
host.apps.launch
host.actions.execute
host.network.scan
host.files.read
host.files.create
host.files.replace
host.desktop.observe
host.desktop.control
host.software.search
host.software.install
host.software.remove
host.exec.one_shot
admin.host.read
admin.host.configure
admin.host.audit
```

Suggested risk posture:

| Action class | Default approval behavior |
|---|---|
| inventory and capability search | no extra approval after authorization |
| local read-only observation | direct explicit request is sufficient |
| workspace-only transformation producing a new file | direct explicit request is sufficient |
| launching an ordinary reviewed app | direct explicit request is sufficient |
| bounded scan of an allowed local CIDR | exact explicit request may be sufficient under owner policy |
| attaching to an existing GUI session | approval required |
| overwriting or deleting user files | approval required |
| package install, remove, upgrade, or downgrade | approval always required |
| public-network scan | approval always required and disabled by default |
| privileged execution profile | approval always required |
| one-shot program execution | approval always required |

The runtime must compute the effective `security_id` from the validated adapter/action. Do not trust a model-supplied risk class or capability identifier.

In a multi-user or shared-tenant deployment, host control is not automatically shared with the archive. Bind a host agent to explicit actor accounts or `own_id` values. Default access should be owner-only. A tenant member who can read shared documents must not thereby gain access to the owner's desktop or package manager.

## Durable jobs, effects, and reconciliation

### Host action lifecycle

Use a durable lifecycle such as:

```text
planned
awaiting_approval
approved
admitted
running
completed
partial
failed
cancelled
unknown
reconciling
reconciled
```

A job can move to `unknown` when the process or package transaction may have crossed an external boundary but Friday lacks a final receipt.

### Minimal `HostActionJob`

```text
job_id
actor_user_id
actor_own_id
conversation_id
source_message_id
host_agent_id
capability_id
adapter_id
adapter_version
action_id
normalized_arguments
plan_digest
risk_class
authorization_basis
approval_id optional
idempotency_key
status
stage
systemd_unit optional
started_at
updated_at
completed_at
result_ref optional
receipt_ref optional
error_code optional
continuation_work_item_id optional
```

Store large output and evidence in files, not as unbounded SQLite text.

### Minimal `HostActionReceipt`

```text
protocol_version
host_agent_id and version
job_id and idempotency_key
plan_digest
adapter and executable attestation
redacted argv rendering and argv hash
filesystem grants
network target snapshot
process/cgroup identity
start and finish times
exit status or signal
timeout, cancellation, and truncation flags
raw evidence refs and hashes
parsed result identity
observed postconditions
effect classification
agent signature
```

### Idempotency

The idempotency key must identify the exact admitted plan, not only the chat message.

- exact retries return the existing job;
- changed arguments or changed executable identity create a conflict;
- package execution is never repeated automatically after an uncertain disconnect;
- action retries after `unknown` require reconciliation or a new explicit instruction;
- cancellation targets the exact cgroup/unit and records whether termination was observed.

### Postconditions

Examples:

```text
software_install
    exact package/version is installed and adapter attestation succeeds

file_transform
    output exists inside the granted workspace, hashes correctly, and is readable

app_launch
    application unit/process is active or a D-Bus activation receipt proves handoff

app_stop
    exact tagged unit/cgroup is inactive

nmap_scan
    process exited, XML parsed, requested target accounting is closed, and coverage is known
```

Do not define “process returned zero” as the universal postcondition.

## Host-agent protocol and transport

### Preferred transport

Use a versioned authenticated protocol over a Unix domain socket created by the user service and bind-mounted into the backend container.

Benefits:

- no LAN listener;
- no dependence on host gateway routing;
- filesystem permission boundary;
- straightforward local deployment;
- easy fail-closed disablement.

Suggested paths:

```text
host:      /run/user/<uid>/friday-host-agent/agent.sock
container: /run/friday-host-agent/agent.sock
```

The deployment installer must create the runtime directory and group/permission mapping deliberately. Do not rely on Docker silently creating a root-owned directory.

A host-gateway TCP fallback may exist for environments where the socket mount is impossible, but it must use mutual TLS or an equivalent pinned authenticated channel and must be disabled by default.

### Request authentication

Even over a Unix socket, use a versioned signed envelope containing:

```text
protocol version
request id
host agent id
monotonic sequence or nonce
timestamp and expiry
method
job id
actor identifiers
idempotency key
plan digest
approval receipt id when required
SHA-256 of the canonical body
HMAC or equivalent signature
```

Reject replay, stale requests, unknown protocol major versions, and host-agent identity mismatch.

The host agent must independently validate the action schema and policy. It does not blindly trust the backend's rendered argv.

### Handshake

The agent health response includes:

```text
protocol versions
agent identity and build
OS distribution and version
kernel architecture
user identity
systemd user-manager availability
graphical-session state
D-Bus, Wayland, X11, portal, and AT-SPI availability
package-manager backends
adapter catalog digest
policy digest
running job count
clock information
```

Agent capabilities are machine state, not permanent memory. Revalidate them at startup and before sensitive execution.

## Deployment on Ubuntu

### Feature flags

Default everything to disabled:

```text
FRIDAY_HOST_CONTROL_ENABLED=0
FRIDAY_HOST_AGENT_SOCKET=/run/friday-host-agent/agent.sock
FRIDAY_HOST_ACTION_MAX_CONCURRENCY=2
FRIDAY_HOST_ACTION_DEFAULT_TIMEOUT_SEC=300
FRIDAY_HOST_ACTION_MAX_OUTPUT_BYTES=8388608
FRIDAY_HOST_PACKAGE_INSTALL_ENABLED=0
FRIDAY_HOST_DESKTOP_CONTROL_ENABLED=0
FRIDAY_HOST_ONE_SHOT_EXEC_ENABLED=0
FRIDAY_HOST_ALLOWED_CIDRS=
FRIDAY_HOST_ALLOWED_PATH_ROOTS=
```

Exact names may follow current configuration conventions. Keep validator behavior strict.

### Compose integration

Provide an optional Compose override that:

- mounts only the host-agent socket directory;
- mounts the shared host-job data directory already under the Friday data root;
- supplies host-control feature flags;
- does not mount `/usr`, `/etc`, `/home`, `/run/user` wholesale, `/var/run/docker.sock`, or the system D-Bus socket;
- preserves read-only root, dropped capabilities, and `no-new-privileges` for the backend.

The backend does not need host executables mounted into the container.

### User service

`friday-host-agent.service` should:

- run as the selected desktop user;
- start independently of the Docker backend;
- support linger for CLI jobs when no graphical session is active;
- report GUI actions unavailable when the graphical session is absent;
- create the socket with controlled permissions;
- use a private runtime and state directory;
- apply `NoNewPrivileges`, task, memory, and file-descriptor limits appropriate to the daemon;
- launch work in separate transient user units rather than weakening the daemon sandbox globally;
- import only the required graphical-session environment when one exists.

### Package broker service

`friday-package-broker` is a root system service or socket-activated service with a tiny API.

- Initial installation requires an explicit local operator setup step.
- The service does not receive Friday's database, model endpoint, Telegram token, or API token.
- The service accepts only signed, expiring package plans through the user agent.
- The service uses a minimal environment and fixed executable paths.
- Its audit is separate from and correlated with Friday's audit by transaction ID.
- Do not configure passwordless arbitrary sudo for the Friday user.

## Result projection and user experience

### Natural language remains primary

Optional commands may exist:

```text
/apps
/apps status
/jobs
/jobs <id>
/jobs cancel <id>
```

Do not require a command mode for normal actions.

### Progress messages

Long actions should emit event-based progress:

```text
Package plan prepared
Waiting for approval
Package transaction started
Package transaction completed
Capability attested
Scan started
12 of 256 targets accounted for
Parsing result
Report ready
```

Do not invent percentages when the underlying tool cannot provide them.

### Bounded model context

Do not inject all raw output or the complete installed-software inventory into the prompt.

Use:

- capability search for top relevant actions;
- structured result schemas;
- bounded warnings and excerpts;
- evidence references for drill-down;
- deterministic summaries for large result sets;
- follow-up tools for exact details.

This matters for the local 27B primary model. A narrow semantic tool is more reliable than a forest of package names and flags.

### Honest response language

Friday must distinguish:

```text
The package plan was approved.
The package transaction started.
The package is installed, but its adapter failed attestation.
The program ran, but the result parser failed.
The scan covered 231 of 256 requested addresses.
The process may still have completed after the connection was lost; I have not reconciled it yet.
The GUI application opened, but this installation exposes no supported control interface.
```

Never compress these into “done” when the evidence is weaker.

## Security requirements

### Forbidden execution surfaces

Reject or keep disabled:

- arbitrary shell strings;
- shell interpreters as one-shot executables;
- terminal emulators as an automation bypass;
- arbitrary Python, Perl, Ruby, Node, PowerShell, or other interpreter execution through host control;
- arbitrary `sudo` or `pkexec`;
- Docker/Podman socket access;
- unrestricted system D-Bus access from the backend container;
- arbitrary systemd unit creation from model fields;
- arbitrary environment injection;
- arbitrary working directories;
- writable executable directories;
- package installation from URLs, chat attachments, local `.deb` files, PPAs, or newly added repositories in v1;
- arbitrary nmap flags or NSE scripts.

A future feature may deliberately add one of these behind a separate reviewed contract. Do not smuggle it into the initial generic action layer.

### Output injection

Application output may contain fake JSON, fake tool calls, ANSI controls, terminal escape sequences, Markdown, XML entities, or strings that resemble Friday protocol markup.

- capture bytes without executing terminal control sequences;
- bound and normalize text before prompt projection;
- parse structured formats with safe parsers and entity expansion disabled;
- never deserialize arbitrary Python objects;
- label every output block as untrusted application evidence;
- preserve raw bytes by hash when needed;
- strip service/tool markup before model exposure;
- prevent application output from choosing the next tool.

### Secrets

- The host agent must not inherit Friday secrets.
- Logs and receipts use redacted argument rendering plus hashes.
- Secrets for an application must be represented by opaque secret references and delivered through an adapter-specific channel.
- Password fields and authentication dialogs are not readable through generic desktop control.
- Clipboard access is disabled by default.

### Network privacy

A local scan can reveal device names, MAC addresses, service banners, and topology. Treat scan evidence as private tenant data. Do not send raw results to a public model or external service without the same private-text policy that governs documents.

### Package supply chain

Package installation is privileged code execution through package maintainer scripts even when the package comes from an official repository. Preserve the high-risk classification, exact origin, exact version, and approval receipt. “From APT” is not a magic safety wand.

## Admin and diagnostics surface

Add a compact Host Control section to Admin UI or diagnostics with:

- enabled/disabled state;
- agent connection, identity, version, and protocol;
- user-session and desktop capability state;
- package broker health;
- adapter catalog and attestation state;
- configured network/path policy;
- running and unknown jobs;
- pending package transactions;
- recent host action audit rows;
- one-shot execution state;
- explicit warnings when privileged or public-network profiles are enabled.

Do not display secrets, complete command stdin, private stdout, or raw desktop text in generic diagnostics.

## Implementation sequence

Build one thin vertical slice at a time.

### Phase 0: contracts and threat review

1. Add versioned request, plan, receipt, and result models.
2. Define capability, risk, approval, target, and path policies.
3. Add feature flags and strict validation.
4. Add fake-agent tests before real host execution.
5. Update `docs/SECURITY.md` with the new trust boundaries.

Exit condition: no process can run yet, but invalid plans, replay, drift, and denied actors fail closed in tests.

### Phase 1: unprivileged host agent and inventory

1. Implement authenticated Unix-socket handshake.
2. Implement agent identity and health.
3. Discover package-owned executables and desktop applications.
4. Add executable attestation.
5. Add `host_capability_search` and `host_capability_describe`.
6. Hide all host tools when disabled or disconnected.

Exit condition: Friday can accurately say what reviewed capabilities are available without launching anything.

### Phase 2: bounded CLI jobs

1. Implement transient systemd user jobs.
2. Implement workspace grants, budgets, output capture, cancellation, and receipts.
3. Implement the nmap adapter with fake XML fixtures first.
4. Implement one local transformation adapter such as `jq` or `ffmpeg`.
5. Add durable HostActionJob and unknown-outcome handling.

Exit condition: preinstalled nmap can perform an authorized local scan and Friday can prove coverage.

### Phase 3: APT package broker

1. Implement APT resolve and canonical planning.
2. Integrate existing approvals.
3. Implement exact-plan execution and drift rejection.
4. Add package transaction receipts and post-install attestation.
5. Resume the original Work Item after installation.

Exit condition: the full “install nmap if absent, then scan” journey passes end to end.

### Phase 4: reusable application capabilities

1. Add built-in ffmpeg, LibreOffice, jq, and MPRIS adapters.
2. Add declarative adapter drafts and operator activation.
3. Add the disabled owner-only one-shot executor.
4. Bind adapters to package/executable versions.

Exit condition: at least one newly installed CLI without an initial built-in adapter can be used once with exact approval and then converted into an approved bounded adapter.

### Phase 5: desktop control

1. Add launch-only support through desktop entries and D-Bus activation.
2. Add app-specific D-Bus/UNO/MPRIS control.
3. Add optional bounded AT-SPI control for Friday-launched windows.
4. Add portal-mediated capture only where needed.

Exit condition: Friday can perform a semantic action in a GUI application and honestly report unsupported control for applications that expose no safe interface.

### Phase 6: optional expansion

Only after the preceding contracts are stable:

- user-level Flatpak;
- Snap backend;
- dedicated tool environments such as `pipx` or `uv tool`;
- privileged isolated network profiles shared with engineer mode;
- additional desktop automation backends;
- remote host agents with mutual TLS and explicit enrollment.

## Required tests

### Contract and policy tests

- unknown protocol version rejected;
- stale nonce and replay rejected;
- plan/body hash mismatch rejected;
- approval for plan A cannot execute plan B;
- model-supplied risk or security ID ignored;
- disabled feature exposes no tool;
- unauthorized actor cannot discover sensitive desktop state;
- shared-tenant non-owner cannot control the host by inheritance.

### Argument and executable tests

- `nmap; rm -rf /` is never treated as a target or package;
- shell metacharacters remain literal data or are rejected by schema;
- executable outside an approved package/path is rejected;
- symlink replacement after planning is detected;
- writable executable replacement is detected;
- PATH changes do not alter executable resolution;
- adapter version drift invalidates the plan;
- package version drift invalidates the approval.

### Filesystem tests

- `..` traversal rejected;
- symlink escape rejected;
- file swapped between grant and execution rejected where identity matters;
- output cannot overwrite input without a replace grant;
- job cannot read another actor's workspace;
- host agent cannot read Friday's SQLite database through a generic action.

### Job and effect tests

- exact retry returns the existing job;
- changed retry conflicts;
- backend restart resumes status without rerunning;
- agent restart reconciles transient units;
- disconnect after process start produces `unknown`, not automatic retry;
- cancellation kills the full cgroup and records observed state;
- timeout and output truncation lower coverage;
- raw output cannot inject a tool call.

### Package tests

- APT simulation and execution plan match;
- repository-origin change invalidates approval;
- unauthenticated package rejected;
- dependency drift invalidates approval;
- lock contention reports a bounded retryable failure without duplicate transaction;
- install receipt proves exact package/version after completion;
- installation success plus adapter failure is reported as partial capability activation;
- broker has no generic command method;
- no sudo password enters logs, database, or prompts.

### nmap tests

- exact private host accepted under policy;
- exact local CIDR accepted under policy;
- public target rejected by default;
- oversized CIDR rejected;
- hostname resolving partly outside policy rejected;
- DNS change between plan and execution detected;
- raw flags cannot be injected through a target string;
- arbitrary NSE script request rejected;
- XML entities disabled;
- malformed or truncated XML returns partial/unavailable result;
- target accounting closes before `complete` coverage;
- ordinary profile runs without root capabilities;
- engineer mode and host control use the same parser and target-normalization tests.

### GUI tests

- no graphical session reports launch/control unavailable;
- launch receipt binds exact desktop entry and PID/cgroup;
- generic control cannot attach to an existing window without approval;
- password roles are redacted;
- terminal and auth-dialog interaction is rejected;
- stale accessibility element is re-resolved before action;
- app-specific D-Bus action records observed postcondition.

## Acceptance battery

The feature is not complete until all of the following demonstrations work on an Ubuntu test host.

### A. Disabled and unavailable behavior

1. `FRIDAY_HOST_CONTROL_ENABLED=0`.
2. Ask Friday to run nmap.
3. Host tools are not shown to the model and no host request occurs.
4. Friday continues normal operation.
5. Enable the feature but stop the user agent.
6. Friday reports host control unavailable without hallucinating execution.

### B. Existing application functional use

1. Install or preinstall `jq` or `ffmpeg` on the test host.
2. Ask Friday to perform a real transformation in a job workspace.
3. Verify exact executable attestation, output file, hash, receipt, and user delivery.
4. Verify the original input is unchanged.

### C. Install and use nmap

1. Ensure `nmap` is absent.
2. Ask: `Install nmap if necessary and scan 192.168.1.0/24.`
3. Friday prepares an exact APT plan and asks for approval once.
4. Rejecting approval causes no package or scan effect.
5. Approve a fresh plan.
6. Broker installs the exact package.
7. Host agent attests the executable and adapter.
8. Friday resumes the original request automatically.
9. The bounded unprivileged scan runs.
10. Friday returns host/service results, raw evidence references, and a coverage statement.
11. Audit links the original message, approval, package transaction, action job, and final response.

### D. Drift and replay

1. Approve a package or action plan.
2. Change the package candidate, executable, adapter version, or target resolution before execution.
3. Friday refuses the stale approval.
4. Replay the old signed request.
5. Host agent rejects it.

### E. Unknown outcome

1. Start a bounded action.
2. Break the backend-agent connection after process admission.
3. Friday records `unknown`.
4. Restoring the connection reconciles the exact systemd unit and evidence.
5. No duplicate process starts.

### F. GUI honesty

1. Launch an app with no supported control interface.
2. Friday may prove launch but must say functional control is unavailable.
3. Launch an MPRIS-capable media player.
4. Friday performs play/pause through the semantic interface and proves the state change.

## Deliverables

The architect should produce:

1. backend host-control package and stable tool contracts;
2. user-session host agent;
3. privileged APT package broker;
4. Ubuntu installation and removal scripts;
5. optional Compose override;
6. systemd user and system units;
7. strict configuration validation;
8. database migration for durable jobs, plans, and receipts;
9. built-in nmap adapter and shared engineer-mode integration;
10. at least one local file-transformation adapter;
11. package planning, approval, execution, and postcondition flow;
12. admin diagnostics;
13. unit, integration, security, restart, and acceptance tests;
14. updated README, deployment documentation, SECURITY model, `.env.example`, and changelog;
15. implementation status document under `outer_sol` after completion.

## Definition of done

This work is done when Friday can receive one natural-language request, discover that the required Ubuntu application is missing, present an exact package transaction, obtain a human approval, install the package through a narrow privileged broker, attest the resulting executable, resume the original task, use the application's actual functionality through a typed adapter, collect evidence, and report a truthful structured result.

For the first release, that sentence must be literally demonstrated with `nmap` and an authorized local subnet.

It is also done only if:

- normal Friday operation remains independent of the host agent;
- no general host shell exists;
- no sudo password or Docker socket is introduced;
- installation and action approvals are plan-bound and replay-safe;
- network scope is code-owned and bounded;
- package and process effects survive restart without duplicate execution;
- an unsupported application produces an honest limitation rather than ceremonial window launching;
- the same host capability architecture supports at least one non-network application action.

The desired result is not “Friday can start programs.” The desired result is “Friday can acquire a reviewed capability, exercise it, observe the consequence, and prove what happened.”
