# Friday Obsidian Integration Architecture and Implementation Plan

> Document ID: FRIDAY-OBS-001  
> Status: External architecture proposal, draft v0.2  
> Repository snapshot: `main`, Friday `0.206.0`, 20 August 2026  
> Scope: one-time Obsidian account onboarding from Telegram, mobile-first Obsidian Sync, per-user Headless profiles, vault and note identity, natural-language Obsidian control through Friday, desktop CLI and companion-plugin transports, note search and editing, properties, links, tasks, templates, Bases, workspace actions, event synchronization, operational-memory integration, implementation phases, and acceptance criteria.  
> Related documents: [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), [`MCP_ARCHITECTURE_OBSERVATION.md`](MCP_ARCHITECTURE_OBSERVATION.md), and [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md).

## Revision 0.2

This revision changes the center of gravity of the integration.

The earlier version treated the desktop Obsidian CLI as the primary first step and left Headless Sync as a later optional adapter. That ordering is appropriate only when Friday and Obsidian run inside the same desktop session.

The intended product experience is mobile-first:

```text
Telegram on the phone
    -> one-time Obsidian connection
    -> Friday maintains an always-on server-side vault checkout
    -> Obsidian Sync delivers changes to the official mobile app
    -> the user continues to command Friday in ordinary Russian
```

Therefore:

1. Per-user Obsidian Headless and Obsidian Sync become the primary always-on transport.
2. Telegram onboarding becomes an early product requirement, not an administrative afterthought.
3. The Obsidian account, remote vault, server-side local checkout, desktop installation, and mobile installation become separate identities.
4. Desktop CLI remains a useful local accelerator.
5. The companion plugin remains the interactive context bridge when Obsidian is open.
6. Obsidian URI remains the mobile navigation mechanism.
7. Browser-session cookie extraction is explicitly rejected as the primary login contract.

## Scope decision

This proposal is functionality-first.

It assumes that enabling Obsidian integration is an explicit deployment and user choice. It does not redesign the sensitive-document boundary, encryption policy, or restricted-data handling described in the other architecture documents.

Existing Friday authorization, actor scope, effect classification, idempotency, audit, and publication contracts should remain in place. Beyond that baseline, the product goal is straightforward:

> A person should be able to connect Obsidian once from Telegram and then use useful Obsidian functionality by talking to Friday in ordinary language.

The integration should feel like one workspace with two interfaces:

```text
Friday
    natural-language intent
    semantic retrieval
    document and conversation context
    multi-step coordination
    model synthesis
    durable Work Items

Obsidian
    notes and folders
    links and backlinks
    properties and tags
    tasks and daily notes
    templates
    Bases
    tabs, workspaces, and native editing
    mobile and desktop clients
```

Friday should not attempt to reproduce Obsidian. Obsidian should not become a second implementation of Friday's memory, provenance, or work-state engine.

## Product requirement

The primary onboarding path begins in Telegram.

A user should be able to send:

```text
/obsidian
```

and receive:

```text
Obsidian is not connected.

[ Connect Obsidian ]
```

The happy path should require only:

```text
1. Send /obsidian.
2. Tap Connect Obsidian.
3. Enter Obsidian account credentials once.
4. Select a vault only when more than one is available.
5. Enter MFA or the vault E2EE password only when the account requires it.
```

After that, slash commands are optional. The normal interface is natural language:

```text
Add this to today's note.

Find the Obsidian note about the Friday retrieval architecture.

Create a task to review the document audit tomorrow.

Save this research result in my Work vault.

Link the note we opened to the document we found earlier.

Open the result in Obsidian on my phone.
```

The user should not have to keep Obsidian open on the phone for Friday to perform ordinary note operations.

## Executive architecture decision

Build the integration as a first-party Friday Organ with four adapters behind one Friday-owned service contract.

```text
1. Headless Sync adapter
   - primary always-on and mobile-first path
   - one isolated Obsidian Headless profile per Friday user
   - one server-side local checkout per connected remote vault
   - continuous Obsidian Sync

2. Vault file adapter
   - performs ordinary note reads and writes against the server-side checkout
   - supports Markdown, properties, links, tasks, templates, and managed regions
   - feeds Friday semantic indexing

3. Desktop CLI adapter
   - controls a running desktop Obsidian installation
   - useful when Friday runs in the same desktop user environment
   - provides native commands, workspaces, panes, active files, and plugin commands

4. Companion plugin adapter
   - event-driven active-note and selection context
   - atomic in-app modifications
   - rename and delete notifications
   - richer workspace behavior
   - optional on mobile and desktop
```

Obsidian URI is a fifth, navigation-only fallback:

```text
open a vault
open a note
open a heading or block
open a search
open or create a daily note
```

The target architecture is:

```text
Telegram user
    -> Friday account
    -> Interaction Control Plane
    -> Obsidian Playbook or direct capability
    -> Friday Obsidian Service
         -> server-side vault file adapter
         -> Headless Sync supervisor
         -> desktop CLI when available
         -> companion plugin when active
         -> Obsidian URI for navigation
    -> typed CapabilityOutcome
    -> Work Item continuation and Completion Gate
    -> one user-visible response
```

## The three channels must not be confused

The integration has three different channels with different jobs.

### Obsidian Sync

```text
Friday server checkout
    <-> remote Obsidian vault
    <-> phone and desktop local vaults
```

This is the persistent delivery backbone.

### Companion plugin

```text
currently open Obsidian application
    <-> Friday
```

This provides live UI context while the app is active.

### Obsidian URI

```text
Telegram or Friday UI link
    -> user tap
    -> mobile or desktop Obsidian opens a target
```

This provides navigation, not background synchronization.

A mobile companion plugin cannot be treated as a permanently connected daemon. The integration must continue to work when the phone is offline or the Obsidian app is closed.

## One-time Telegram onboarding

### Entry point

The command:

```text
/obsidian
```

opens a status panel.

Before connection:

```text
Obsidian: not connected

[ Connect Obsidian ]
[ What is required? ]
```

After connection:

```text
Obsidian: connected
Account: user@example.com
Default vault: Work
Sync: ready
Last successful server sync: 21:43

[ Change vault ]
[ Reconnect ]
[ Disconnect ]
```

### Official Telegram clients

The preferred button opens a Telegram Mini App.

The Mini App sends Telegram `initData` to Friday. Friday validates that signed payload and binds the setup session to the existing Friday user and Telegram identity.

No separate Friday password is required.

### Unofficial Telegram clients

The bot must also include a normal HTTPS fallback link:

```text
https://friday.example/connect/obsidian/<single-use-token>
```

The token is:

```text
single use
short lived
bound to one Friday user
bound to the requesting Telegram identity and chat context
scoped only to obsidian.connect
invalidated after completion or cancellation
```

The browser page does not require a second Friday login because the setup token already identifies the requesting Friday account.

Mini App support must never be the only door.

## Obsidian login contract

### Friday does not reuse a random browser cookie

The integration should not depend on extracting or importing an existing `obsidian.md` browser session.

The current official Headless authentication contract is:

```text
ob login
```

with interactive email, password, and automatic MFA prompting when enabled.

There is currently no documented OAuth authorization-code flow, device-code flow, or supported browser-session import for Obsidian Headless.

Therefore the stable onboarding path is a web wrapper around the official Headless login process, not cookie transplantation.

### Interactive login broker

For each Friday user, Friday creates an isolated Headless profile and launches `ob login` through a bounded pseudo-terminal broker.

```text
Telegram Mini App or HTTPS page
    -> login session
    -> PTY broker
    -> ob login
    -> isolated Headless profile
```

The browser displays the prompts produced by the current login state:

```text
Email
Password
MFA code when requested
```

The password and MFA code are sent to the live login process. They are not placed in command-line arguments, Work Items, audit bodies, ordinary logs, or the main Friday database.

After successful login, the durable Obsidian session belongs to the isolated Headless profile.

### Remote vault selection

Friday runs:

```text
ob sync-list-remote
```

and obtains all remote vaults available to the account, including shared vaults.

Selection behavior:

```text
zero remote vaults
    -> explain that an Obsidian Sync vault is required

one remote vault
    -> select automatically and ask for one final confirmation only if needed

multiple remote vaults
    -> show one compact selection screen
```

The selected remote vault may become the user's default logical vault.

### E2EE password

The Obsidian account password and a remote vault's end-to-end encryption password are separate credentials.

If `ob sync-setup` requests a vault password, the onboarding page displays one additional prompt and feeds it to the interactive process.

The E2EE password is not stored in Friday's main database. The Headless client stores the session material it requires for later synchronization inside the user's isolated profile.

### Initial synchronization

Friday creates a per-user local checkout and runs:

```text
ob sync-setup \
  --vault <remote-vault-id> \
  --path <local-checkout> \
  --device-name Friday
```

Then:

```text
ob sync --path <local-checkout> --continuous
```

When initial synchronization reaches a usable state, the connection becomes `ready`.

### Onboarding state machine

```text
not_connected
    -> issuing_setup_token
    -> awaiting_login
    -> awaiting_mfa
    -> listing_remote_vaults
    -> selecting_remote_vault
    -> awaiting_e2ee_password
    -> configuring_local_checkout
    -> initial_sync
    -> ready

ready
    -> reauthentication_required
    -> sync_degraded
    -> sync_error
    -> reconnecting
    -> disconnecting
    -> disconnected
```

Every state is resumable within the setup-session lifetime.

A failed MFA code does not create a second Obsidian connection.

A browser refresh does not restart `ob login` blindly. It reconnects to the existing bounded setup session or creates a fresh one after explicit expiry.

## Minimum user movement

The product should optimize for the following happy path:

```text
/obsidian
    -> tap one button
    -> enter Obsidian email and password
    -> done when one vault and no extra prompt exist
```

Conditional extra steps:

```text
MFA enabled
    -> enter code

multiple remote vaults
    -> choose one

E2EE remote vault
    -> enter vault encryption password
```

These steps cannot be removed without an official account delegation flow from Obsidian.

If Obsidian later publishes OAuth or a device-code flow, the `ObsidianAccountAuthenticator` can adopt it without changing the rest of the account, vault, or operation model.

## Mobile-first topology

The default always-on deployment is:

```text
┌──────────────────────────────────────┐
│ Friday                               │
│                                      │
│ Work Items                           │
│ semantic search                      │
│ Obsidian operations                  │
└─────────────────┬────────────────────┘
                  │ local checkout
┌─────────────────▼────────────────────┐
│ Obsidian Headless                    │
│ per-user profile                     │
│ continuous Sync                      │
└─────────────────┬────────────────────┘
                  │
          Obsidian remote vault
                  │
      ┌───────────┴───────────┐
      │                       │
┌─────▼──────┐          ┌─────▼──────┐
│ Phone      │          │ Desktop    │
│ Obsidian   │          │ Obsidian   │
│ mobile     │          │ app        │
└─────┬──────┘          └─────┬──────┘
      │                       │
      └──── optional companion plugin
```

The phone and the Friday host are separate Sync clients.

```text
mobile Obsidian session
    != desktop Obsidian session
    != browser session
    != Friday Headless session
```

They may belong to the same Obsidian account and remote vault, but each client authenticates independently.

### Mobile prerequisite

For the official, low-friction, always-on route:

> Mobile-first Friday integration requires an active Obsidian Sync subscription and a mobile Obsidian vault connected to the same remote vault.

The user performs the ordinary Obsidian mobile setup once:

```text
install or open Obsidian mobile
sign in inside Obsidian mobile
connect a local mobile vault to the same remote vault
enter the E2EE password if that vault requires it
```

Friday cannot perform this device-local Obsidian step remotely.

### Server-side operation flow

Example:

```text
User in Telegram:
    Add the result to today's note.

Friday:
    resolve account and default logical vault
    resolve today's note path
    modify the server-side local checkout
    verify the new file revision
    wait for or observe server-side Sync completion
    report completion

Obsidian mobile:
    downloads the change on its next Sync cycle
```

Friday may truthfully say:

```text
The note was updated and synchronized from Friday's side.
```

Friday must not claim:

```text
The phone has already downloaded and displayed it.
```

The phone may be offline or the app may not have synchronized yet.

## One-tap mobile opening

After locating or creating a note, Friday may return:

```text
[ Open in Obsidian ]
```

using an Obsidian URI such as:

```text
obsidian://open?vault=Work&file=Projects%2FFriday
```

The user taps the link and the official mobile application opens the target note.

### Device-local vault identity caveat

An Obsidian URI targets a local vault name or local vault ID. A remote Sync vault and a phone's local vault are not the same identity.

Friday therefore models device-local aliases explicitly:

```text
logical vault: Work
remote vault: remote_123
Friday checkout: local_server_456
phone local vault name: Work
phone local vault ID: optional until companion pairing
```

Without a mobile companion plugin, Friday may assume that the mobile local vault name matches the remote vault name. That is a convenience assumption, not a durable identity guarantee.

If opening fails, the user can set one mobile vault alias once. A paired mobile companion plugin can report the exact local vault ID automatically.

## Identity model

The word "vault" refers to several different objects. They must not be collapsed into one field.

```text
FridayUser
    -> ObsidianAccountConnection
        -> ObsidianHeadlessProfile
        -> ObsidianLogicalVault
            -> ObsidianRemoteVaultBinding
            -> FridayLocalVaultCheckout
            -> ObsidianDeviceVaultBinding[]
```

### Friday user

The durable Friday identity that owns Work Items and Obsidian connections.

### Telegram identity

The transport identity that initiated pairing. It is a login bootstrap and notification route, not the canonical Obsidian owner key.

### Obsidian account connection

The durable fact that one Friday user has an authenticated Headless profile.

```python
ObsidianAccountConnection(
    id="obsconn_...",
    owner_id="friday_user_...",
    headless_profile_id="obshp_...",
    account_display="user@example.com",
    account_state="authenticated",
    session_epoch=4,
    connected_at="...",
    last_probe_at="...",
)
```

Friday may retain a display email or account label returned by `ob login`. It does not need the user's password after onboarding.

### Headless profile

An isolated Obsidian Headless configuration and credential store owned by one Friday account.

```python
ObsidianHeadlessProfile(
    id="obshp_...",
    owner_id="friday_user_...",
    profile_root="...",
    state="ready",
    headless_version="...",
)
```

A Headless profile must never be shared by unrelated Friday users.

### Logical vault

Friday's human-facing identity for "my Work vault".

It survives local path changes and may have multiple device-local realizations.

### Remote vault

The Obsidian Sync identity returned by `ob sync-list-remote`.

### Friday local checkout

The local server-side directory synchronized by Obsidian Headless and modified by Friday.

### Device vault binding

A mobile or desktop local vault associated with the same logical vault.

```python
ObsidianDeviceVaultBinding(
    logical_vault_id="obslv_...",
    installation_id="obsinst_phone_...",
    local_vault_id="optional",
    local_vault_name="Work",
    platform="android",
)
```

## Account change and reauthentication

If the stored Headless session expires:

```text
account_state = reauthentication_required
```

Friday keeps the logical vault and local checkout records but refuses account-dependent Sync operations until reauthentication completes.

If the user intentionally switches Obsidian accounts:

1. stop continuous Sync workers;
2. unlink the affected local checkouts;
3. increment the account `session_epoch`;
4. run a new login flow;
5. list remote vaults again;
6. require explicit rebinding when a previous remote vault is unavailable or ambiguous.

Friday must not silently treat a vault with the same display name under another account as the same remote vault.

## Disconnect behavior

The `/obsidian` panel should support disconnecting one vault or the whole account.

Account disconnect:

```text
stop all continuous Sync processes
run ob sync-unlink for bound checkouts
run ob logout for the isolated profile
revoke setup and plugin sessions
mark account connection disconnected
preserve or delete local checkouts according to explicit user choice
```

Vault disconnect:

```text
stop one Sync process
unlink one checkout
remove the remote binding
preserve other account and vault connections
```

Disconnecting the account does not delete the user's remote Obsidian vault.

## Deployment topologies

### Topology A: always-on and mobile-first

```text
Friday service
    -> Headless profile
    -> server-side local checkout
    -> Obsidian Sync
    -> mobile and desktop clients
```

This is the primary product topology.

### Topology B: same desktop session

```text
Friday
    -> official Obsidian CLI
    -> running desktop application
    -> optional Obsidian Sync
```

This is the simplest local developer and single-machine topology.

### Topology C: hybrid

```text
Friday Headless checkout
    -> continuous Sync

Desktop or mobile companion plugin
    -> active note, selection, workspace, and UI actions
```

This provides always-on background operations plus rich foreground interaction.

### One Sync mechanism per device

Do not use desktop-app Sync and Headless Sync against the same vault on the same machine. Obsidian's official Headless documentation warns that running both Sync mechanisms on one device can create conflicts.

A Friday host should use Headless Sync for its checkout. A user's desktop application on another device may use desktop Sync normally.

## Why the current Friday code is a useful starting point

### Human-readable Markdown projection

[`friday/memory/__init__.py`](../friday/memory/__init__.py) already implements a filesystem projection of Knowledge Objects into Markdown. It has:

- stable identity in the filename suffix;
- YAML frontmatter;
- tags, lifecycle, version, entity, provenance, and timestamps;
- human-readable titles and summaries;
- Obsidian-style wikilinks to entities;
- atomic replacement;
- orphan pruning;
- a clear statement that SQLite remains the source of truth.

This projection should remain available as a read-oriented Friday knowledge mirror.

It is not sufficient as the interactive integration because its README explicitly states that edits will be overwritten on the next synchronization. A user-facing Obsidian integration needs writable notes, note identity across renames, conflict handling, account and remote-vault binding, active-editor context, and incremental events.

### Friday Organ Protocol

[`docs/ORGANS.md`](../docs/ORGANS.md) already defines code-owned extension modules with capabilities, workers, and routers. Obsidian is a good Organ candidate because it is optional, contributes an API surface, supervises long-lived Sync processes, receives plugin sessions, and should not enlarge the central agent runtime.

### Execution and orchestration boundaries

Friday already has:

- capability-gated tools;
- execution-kernel ownership of effects;
- V12 typed planning contracts;
- durable missions;
- the proposed Interaction Control Plane and Work Items;
- document, conversation, graph, and generated-file services.

The Obsidian integration should attach to these boundaries rather than build a separate agent loop.

## Recommended repository structure

Server-side Organ:

```text
friday/organs/obsidian/
    __init__.py
    contracts.py
    models.py
    service.py
    router.py
    tools.py
    playbooks.py

    onboarding/
        service.py
        tokens.py
        pty_broker.py
        state_machine.py
        mini_app.py

    account/
        profiles.py
        authenticator.py
        registry.py

    sync/
        headless.py
        supervisor.py
        status.py
        reconciliation.py

    vault/
        editor.py
        markdown.py
        frontmatter.py
        links.py
        tasks.py
        templates.py
        bases.py
        daily_notes.py
        identity.py
        merge.py

    transports/
        desktop_cli.py
        plugin.py
        uri.py

    indexer.py
    diagnostics.py
```

Companion plugin:

```text
integrations/obsidian-friday/
    manifest.json
    package.json
    tsconfig.json
    esbuild.config.mjs
    src/
        main.ts
        settings.ts
        connection.ts
        pairing.ts
        protocol.ts
        commands.ts
        events.ts
        note-operations.ts
        friday-view.ts
        status-bar.ts
```

Mini App and fallback onboarding UI may live inside Friday's existing UI package or as a small dedicated frontend.

## JOP extension required for natural-language tools

The current Friday Organ Protocol exposes capabilities, workers, and HTTP routers, but not a first-class tool-provider extension.

Obsidian is a strong justification for a small JOP extension rather than wiring many handlers into the legacy runtime.

```python
class Organ:
    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return ()

    def tools(self, ctx: ServiceContext) -> Sequence[ToolRegistration]:
        return ()

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return ()

    def router(self) -> APIRouter | None:
        return None
```

A `ToolRegistration` should contain:

```text
Friday-owned ToolSpec
handler
effect class
required capability
input and output revisions
```

The Organ registry contributes these registrations to the existing Execution Kernel during startup.

This extension will also benefit future SaaS and MCP-backed Organs while preserving JOP's explicit-registration rule.

## Service and adapter model

The model-visible capabilities call one `ObsidianService`.

```python
class ObsidianService:
    async def execute(
        self,
        operation: ObsidianOperation,
        *,
        actor: ActorContext,
        absolute_deadline: float,
    ) -> ObsidianOperationResult: ...
```

The service separates two concerns.

### Vault operation adapter

Performs the actual note or workspace action:

```text
server-side file adapter
desktop CLI adapter
companion plugin adapter
URI navigation adapter
```

### Sync adapter

Moves file changes between the Friday checkout and the Obsidian remote vault:

```text
Headless Sync adapter
```

A successful local write and a successful remote Sync are distinct postconditions.

Example outcome:

```python
ObsidianOperationResult(
    status="success",
    local_write="confirmed",
    server_sync="confirmed",
    mobile_delivery="not_observed",
    path="Projects/Friday.md",
    revision="sha256:...",
)
```

## Headless account and Sync adapter

### Supported operations

```text
ob login
ob logout
ob sync-list-remote
ob sync-list-local
ob sync-setup
ob sync
ob sync --continuous
ob sync-status
ob sync-unlink
ob sync-config
```

### Process supervision

One long-lived Sync worker is supervised per connected checkout.

The supervisor records:

```text
process identity
Headless version
profile identity
logical and remote vault identity
checkout path
start time
last healthy status
last successful sync
restart count
current error class
```

Crash recovery:

```text
unexpected exit
    -> status degraded
    -> bounded restart policy
    -> probe sync status
    -> reconcile local and remote state
    -> ready or operator-visible error
```

### Selective synchronization

The adapter may configure:

```text
bidirectional mode
conflict strategy
attachment types
configuration categories
excluded folders
device name
```

These are per-vault settings stored in Friday's vault configuration.

### Headless is the Sync transport, not the full Obsidian UI

Headless does not replace desktop Obsidian commands and workspace behavior.

Friday performs ordinary note operations against the synchronized local checkout. The desktop CLI or companion plugin is required for features that inherently depend on a running application, current selection, tabs, panes, or community-plugin commands.

## Server-side vault file adapter

This adapter provides the mobile-first functional core even when no desktop Obsidian process is running.

### Supported core operations

```text
list and search files
read note
create note
append and prepend
replace with expected revision
move and rename with link reconciliation
delete and trash policy
read and update YAML properties
read and update tags
parse outgoing links
compute backlinks from the indexed vault
read and update tasks
resolve daily note path
apply templates
read and generate supported Base files
managed-region updates
```

### Atomicity

Writes use:

```text
read current revision
construct updated content
write temporary file in the same directory
fsync where required
atomic replace
return new revision
```

### Rename and move

A raw filesystem rename does not automatically provide every Obsidian behavior.

The service therefore treats rename and move as semantic operations:

```text
resolve the exact source
verify destination absence
compute inbound link rewrite plan
rename or move the file
rewrite affected links where configured
verify all changed revisions
return a multi-file outcome
```

When a companion plugin or desktop CLI is available, Friday may delegate rename to Obsidian so the application performs its native link-update behavior.

When neither is available and the backlink index is incomplete, Friday returns `partial` or `needs_input` instead of claiming that all references were updated.

### Daily note configuration

The server-side adapter needs:

```text
daily note folder
date format
template path or template folder
time zone
```

Friday should attempt to discover these values from the synchronized vault configuration when possible. During onboarding it asks the user only when discovery is missing or ambiguous.

A sensible zero-configuration fallback may be offered, but it must be visible in the connection summary.

## Desktop Obsidian CLI adapter

The official Obsidian CLI controls the desktop application and provides a broad native surface.

It is the preferred adapter when Friday runs in the same desktop user environment.

### Useful capabilities

```text
search and read
create, append, prepend, move, and delete
properties and tags
daily notes
templates
tasks
links and backlinks
Bases
commands and plugin commands
tabs and workspaces
active file
open and focus actions
```

### Execution rules

The adapter should:

- invoke the binary without a shell;
- pass every argument separately;
- target vault by exact ID when available;
- prefer exact `path=` after resolution;
- request structured output where supported;
- enforce deadlines and output caps;
- normalize output into Friday contracts;
- probe the Obsidian and CLI version;
- cache a command and capability manifest by version;
- mark the adapter unavailable when the observed contract is incompatible.

### Topology limitation

The CLI requires access to the desktop user's Obsidian installation and application session. It should not be stretched into the primary server/mobile solution.

## Companion plugin adapter

The companion plugin opens an outbound connection to Friday.

```text
Obsidian companion plugin
    -> authenticated WebSocket
    -> Friday Obsidian Organ
```

An outbound plugin connection works for:

- Friday in Docker;
- Friday under another local service account;
- Friday on another reachable machine;
- mobile foreground interaction;
- desktop foreground interaction.

### Handshake

```json
{
  "protocol": "friday.obsidian.v1",
  "plugin_version": "0.1.0",
  "obsidian_version": "1.12.7",
  "installation": {
    "id": "obsinst_...",
    "platform": "android",
    "device_name": "Phone"
  },
  "vault": {
    "local_id": "ef6ca3e3b524d22f",
    "name": "Work",
    "logical_vault_id": "obslv_..."
  },
  "capabilities": [
    "note.read",
    "note.create",
    "note.process",
    "note.rename",
    "note.delete",
    "properties",
    "links",
    "commands",
    "workspace",
    "selection",
    "events"
  ],
  "commands_digest": "sha256:..."
}
```

### Plugin pairing

Plugin pairing is separate from Obsidian account onboarding.

```text
Obsidian account onboarding
    -> lets Friday operate and synchronize the vault

companion plugin pairing
    -> lets the open app share active UI context with Friday
```

The plugin displays a short pairing code or opens a Friday pairing link. The code binds the Obsidian installation and local vault to the already connected Friday logical vault.

The plugin never needs the user's Obsidian password.

### Plugin responsibilities

The plugin should:

- use Obsidian Vault APIs for note operations;
- use `Vault.process()` for read-modify-write operations;
- listen for create, modify, rename, and delete events;
- expose the current file, heading, selection, active leaf, and workspace state;
- report the exact local vault ID and name for deep links;
- list available command IDs;
- execute explicit command IDs;
- register Friday-facing command-palette actions;
- maintain a reconnecting outbound session while the app is active;
- deduplicate operation IDs;
- acknowledge events after Friday accepts them.

### Mobile limitation

The mobile plugin is an interactive foreground bridge. Friday must not rely on its WebSocket being alive while the app is backgrounded or closed.

## Obsidian URI adapter

Obsidian URI is useful for navigation-only actions:

```text
open a vault
open a note
open a heading or block
create or append to a note
open the daily note
open search with a query
```

URI invocation should not be treated as proof that a write completed or that the user viewed the note.

For a Telegram response, the ideal pattern is:

```text
Found: Friday Retrieval Architecture

[ Open in Obsidian ]
```

## Browser session bridge position

A browser extension could detect an already authenticated `obsidian.md` session and help the user reach account or Sync pages.

It should not be part of the required architecture.

Allowed future role:

```text
observe signed-in account state
open the correct Obsidian setup page
assist pairing
never export raw cookies
never act as the primary vault transport
```

Rejected primary contract:

```text
copy browser cookies into Friday
call undocumented internal web endpoints
pretend the browser session is a Headless session
```

The login provider remains replaceable so an official OAuth or device-code flow can be adopted later.

## Vault registry

Friday needs a durable registry independent of live transport sessions.

```python
@dataclass(frozen=True, slots=True)
class ObsidianLogicalVault:
    id: str
    owner_id: str
    display_name: str
    account_connection_id: str
    remote_vault_id: str
    remote_vault_name: str
    server_checkout_id: str
    default: bool
    default_capture_folder: str
    daily_note_enabled: bool
    browse_roots: tuple[str, ...]
    index_roots: tuple[str, ...]
    inbox_roots: tuple[str, ...]
    managed_roots: tuple[str, ...]
    enabled: bool
```

### Multiple vaults

Friday should support:

- one default logical vault per user;
- explicit named selection;
- a remembered active vault inside a Work Item;
- per-vault folder defaults;
- multiple remote vaults under one account;
- shared remote vaults;
- multiple device-local aliases.

Natural-language examples:

```text
Save this to my Work vault.

Search the Personal vault instead.

Use Archive only for this task.

Make Work the default for daily notes.
```

## Note identity

Obsidian addresses notes by vault-relative path, but paths change.

### Managed and linked notes

Friday adds a stable property:

```yaml
friday_obsidian_id: obnote_7d18d2f4c9e44a35
```

Optional properties:

```yaml
friday_object_id: ko_...
friday_raw_object_id: raw_...
friday_projection_kind: linked
friday_projection_revision: 4
```

### Unmodified user notes

Friday does not require injecting a property merely to read or search a note.

Before durable binding, the observed identity is:

```text
logical_vault_id + exact path + observed revision
```

A durable binding is created when:

- the user asks Friday to link the note;
- Friday creates the note;
- Friday creates a managed region;
- indexed mode is configured to assign IDs;
- a companion plugin reports an existing integration ID.

### Identity invariants

- title is not identity;
- path is not permanent identity after binding;
- content digest is a revision, not identity;
- a copied note receives a new integration identity;
- rename and move update the binding;
- deletion tombstones the binding;
- restore or recreation is reconciled explicitly.

## Note ownership and editing modes

### User-owned

The note is entirely user-authored. Friday modifies it only after an explicit request.

### Linked

The note is user-owned, but Friday may modify selected properties and marked managed regions.

### Friday-managed

Friday owns the complete body and may regenerate it from a Friday object or Work Item outcome.

### Projection

The note is a rebuildable mirror such as the current `MemoryVault`. Edits are not imported.

### Inbox note

The note is user-owned and selected for explicit ingestion into Friday's review pipeline.

## Managed regions

For linked notes, Friday should update marked regions rather than rewrite the complete file.

```markdown
<!-- friday:managed:start id="summary" revision="4" -->
## Friday summary

Generated content here.
<!-- friday:managed:end id="summary" -->
```

Rules:

- region IDs are unique within a note;
- Friday updates only the selected region;
- content outside managed markers is preserved;
- malformed or duplicate markers return `ambiguous`;
- every successful write returns a new revision digest.

Useful region kinds:

```text
summary
source-links
related-documents
conversation-capture
research-result
review-status
action-items
```

## Revision and conflict model

Every read result includes:

```text
logical vault ID
path
integration ID if present
revision digest
modified time
server Sync state
```

Every destructive replacement accepts `expected_revision`.

Possible outcomes:

```text
success
unchanged
not_found
ambiguous
conflict
invalid_note
unsupported
unavailable
partial
uncertain
failed
```

Default policy:

```text
append or prepend
    -> apply to the current note through an idempotent operation

property update
    -> update current frontmatter atomically

managed region update
    -> merge into current body

full replacement with stale revision
    -> conflict

rename target already exists
    -> conflict

uncertain transport failure
    -> reconcile before retry
```

## Operation durability and recovery

Store each mutation before dispatch:

```text
operation_id
work_item_id
logical_vault_id
method
arguments digest
expected revision
operation adapter
sync requirement
status
attempt
local result revision
server sync result
created_at
updated_at
```

State machine:

```text
prepared
    -> dispatched
    -> local_succeeded
    -> sync_pending
    -> succeeded
    -> failed
    -> uncertain
    -> reconciled
    -> cancelled
```

Retry rules:

- reads may retry after transport failure;
- create uses operation identity or target reconciliation;
- append uses a deduplication marker or postcondition;
- move and rename inspect source and destination;
- delete never blindly retries after an uncertain outcome;
- UI commands without observable postconditions remain `uncertain` after disconnect.

## Friday-owned capability surface

Do not publish the complete CLI or plugin command catalog directly to the model. Expose stable Friday capabilities.

### Connection and status

```text
obsidian_connection_status
obsidian_list_vaults
obsidian_select_default_vault
obsidian_sync_status
```

The conversational model should not receive raw login credentials or drive the authentication prompt.

### Discovery and navigation

```text
obsidian_list_notes
obsidian_search_notes
obsidian_open_note
obsidian_open_search
obsidian_recent_notes
```

### Note content

```text
obsidian_read_note
obsidian_create_note
obsidian_append_note
obsidian_prepend_note
obsidian_replace_note
obsidian_update_managed_region
obsidian_move_note
obsidian_delete_note
```

### Metadata and graph

```text
obsidian_get_properties
obsidian_set_properties
obsidian_remove_properties
obsidian_list_tags
obsidian_get_backlinks
obsidian_get_outgoing_links
obsidian_list_unresolved_links
obsidian_list_orphans
obsidian_list_deadends
```

### Daily notes, templates, and tasks

```text
obsidian_daily_note
obsidian_create_from_template
obsidian_list_templates
obsidian_list_tasks
obsidian_update_task
```

### Bases and workspace

```text
obsidian_list_bases
obsidian_query_base
obsidian_create_base_item
obsidian_create_or_update_base
obsidian_list_workspaces
obsidian_load_workspace
obsidian_save_workspace
```

Workspace operations require a desktop CLI or active companion plugin.

### Expert command bridge

```text
obsidian_list_commands
obsidian_run_command
```

`obsidian_run_command` requires an exact command ID returned by the current desktop or plugin command catalog.

### Effect classes

Read-only:

```text
list, search, read, backlinks, tags, tasks, Base query, command listing
```

Mutating:

```text
create, append, properties, task update, move, managed-region update, workspace load
```

High-impact:

```text
delete, permanent delete, full overwrite, plugin install or uninstall, publish, Sync restore, arbitrary expert command without a known behavior profile
```

## Typed capability outcomes

The model receives normalized outcomes, not raw subprocess output.

Search example:

```python
CapabilityOutcome(
    status="success",
    result_type="obsidian.note_candidates.v1",
    items=(
        ObsidianNoteCandidate(
            logical_vault_id="obslv_...",
            path="Projects/Friday.md",
            title="Friday",
            excerpt="...",
            score=0.91,
            match_kinds=("native_text", "property", "semantic"),
            revision="sha256:...",
        ),
    ),
    coverage=CoverageReport(
        complete=True,
        sources=("vault_text_search", "friday_semantic_index"),
    ),
)
```

Write example:

```python
CapabilityOutcome(
    status="success",
    result_type="obsidian.note_write.v1",
    postcondition={
        "logical_vault_id": "obslv_...",
        "path": "Projects/Friday.md",
        "revision": "sha256:...",
        "operation": "append",
        "local_write": "confirmed",
        "server_sync": "confirmed",
        "mobile_delivery": "not_observed",
    },
)
```

## Search architecture

Friday and Obsidian have different strengths.

### Vault-native lanes

Use the synchronized local vault or desktop Obsidian for:

- exact and lexical text search;
- path and folder filtering;
- property queries;
- tags;
- tasks;
- backlinks and outgoing links;
- unresolved links;
- orphan and dead-end reports;
- Bases queries.

### Friday lanes

Use Friday for:

- semantic search over configured roots;
- approximate content;
- approximate dates;
- cross-corpus search with Friday documents and conversations;
- entity and project resolution;
- reranking;
- continuation from an active Work Item.

### Unified result

```text
query
    -> exact path and title lane
    -> vault lexical search
    -> property and tag lanes
    -> optional Friday semantic index
    -> deduplicate by logical vault and note identity
    -> rank fusion
    -> typed candidate set
```

Explain match channels:

```text
exact title
path
text
property
tag
backlink
semantic passage
recent activity
```

### Avoiding duplicate Friday material

The current `MemoryVault` projection and any Friday-managed export roots are excluded from automatic re-ingestion by default.

```text
Friday Knowledge Object
    -> Markdown projection
    -> Obsidian or vault indexing
    -> never becomes independent Friday evidence
```

Projection notes may be searched and opened. They are marked `friday_projection`.

## Obsidian note indexing in Friday

Indexed mode creates a rebuildable source projection, not a Knowledge Object.

```text
obsidian_note_index
    logical_vault_id
    integration_id nullable
    path
    revision
    title
    aliases
    properties
    tags
    headings
    text passages
    embedding revision
    indexed_at
    deleted_at
```

Stable source reference:

```text
obsidian:<logical-vault-id>:<integration-id>
```

Before binding:

```text
obsidian:<logical-vault-id>:path:<normalized-path>
```

On rename, update the path while preserving integration identity.

On modification, re-index only the changed note.

On delete, invalidate passages and embeddings.

### Browse versus ingest

Reading an Obsidian note for the current Work Item does not make it canonical Friday knowledge.

Explicit import routes:

```text
Remember this note.
Send this note to Friday Inbox.
Move note into a configured Friday Inbox folder.
Obsidian command: Ingest current note into Friday.
```

All use Friday's ordinary ingestion and review pipeline.

## Friday object and Obsidian note bindings

Support durable links between:

```text
Friday Raw Object
Friday Knowledge Object
Friday document catalog entry
Friday entity
Friday conversation
Friday Work Item outcome

and

Obsidian note
Obsidian heading
Obsidian block
Obsidian Base
```

```python
ObsidianBinding(
    id="obsbind_...",
    logical_vault_id="obslv_...",
    note_id="obnote_...",
    note_path="Projects/Friday.md",
    subpath="#Retrieval",
    friday_object_kind="knowledge_object",
    friday_object_id="ko_...",
    relation="describes",
)
```

Relations:

```text
describes
annotates
summarizes
source_for
related_to
result_of_work_item
projects
```

## Daily notes

Daily notes are among the highest-value early capabilities.

Friday should support:

```text
open today's daily note
read today's daily note
append a capture
prepend a briefing
add a task
add a completed-work summary
create or open a note for a requested date
```

Example:

```markdown
## Friday

- 21:10 Captured [[Friday Architecture]]
- [ ] Review document retrieval audit

<!-- friday:managed:start id="daily-summary-2026-08-20" revision="1" -->
### Friday work summary

...
<!-- friday:managed:end id="daily-summary-2026-08-20" -->
```

User text outside managed regions remains untouched.

## Templates

Friday should:

- discover or configure the template folder;
- list templates;
- create a note from a selected template;
- fill explicitly named placeholders;
- preserve unknown template syntax;
- return `needs_input` when required values are missing.

Example playbook:

```text
Create a meeting note from my Architecture Review template using what we discussed.

summarize current discussion
    -> resolve template
    -> identify required fields
    -> ask only for missing fields
    -> create note
    -> verify local revision and Sync state
    -> return an Open in Obsidian action
```

## Properties and tags

Use typed property mutations:

```text
text
list
number
checkbox
date
datetime
```

```python
PropertyMutation(
    name="status",
    action="set",
    value="review",
    value_type="text",
)
```

A multi-property update is one atomic operation.

## Links and backlinks

Return links as structured data:

```text
source path
resolved target path if any
link text
subpath
count
resolved or unresolved
```

Useful workflows:

```text
show backlinks
find notes with no incoming links
find dead ends
resolve an ambiguous wikilink
add links to related Friday documents
create an index note from a candidate set
```

## Tasks

Support:

```text
list incomplete tasks
list tasks in the active or daily note
create a task
mark a task done or todo
set a custom status
open the task's note and location
```

A task reference includes:

```text
logical vault ID
path
line or stable block identifier
observed revision
text excerpt
status
```

Line numbers alone are unstable. Prefer block IDs; otherwise reconcile by task text and nearby context.

## Bases

Treat Bases as a first-class feature.

### Core stage

```text
list Bases
list views
query a view
create a note through a Base
generate a supported .base file from a typed specification
```

```python
BaseSpec(
    name="Friday Active Notes",
    source_folder="Projects",
    filters=(
        PropertyEquals("project", "Friday"),
        PropertyNotEquals("status", "done"),
    ),
    columns=("file.name", "status", "due", "updated"),
    sort=(Sort("due", "asc"),),
)
```

### Plugin stage

The companion plugin may register a custom Friday Bases view for:

```text
Friday documents linked to notes
Friday Work Items captured in Obsidian
notes awaiting Friday ingestion
related documents and conversations
semantic matches for the selected row
```

## Workspace and UI actions

Desktop CLI and companion plugin may support:

```text
open in current tab
open in new tab
open in split
open in new window where supported
load workspace
save workspace
list recent notes
focus Friday companion view
```

These are UI effects. Opening a note does not prove that the user read it.

## Expert command bridge

The command bridge allows Friday to use core and community-plugin commands without custom integration for each plugin.

```text
1. list current command IDs and names
2. resolve by exact ID or unambiguous display name
3. record the command-catalog digest
4. execute one command
5. observe postconditions when available
```

```python
ObsidianCommandProfile(
    command_id="some-plugin:command",
    display_name="Some Plugin: Do thing",
    effect="mutate",
    requires_active_file=True,
    supports_postcondition=False,
)
```

Unknown commands remain expert operations and may finish as `uncertain`.

## Interaction Control Plane integration

The integration should use Work Items and Active Frames from [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md).

### Active Obsidian frame

```python
ObsidianActiveFrame(
    account_connection_id="obsconn_...",
    logical_vault_id="obslv_...",
    active_note_id="obnote_...",
    active_path="Projects/Friday.md",
    active_revision="sha256:...",
    active_heading="#Retrieval",
    selected_candidate_set_id="candset_...",
    last_operation_id="obsop_...",
)
```

This resolves follow-ups:

```text
Add that there.
Open the second one.
Move it to Archive.
Now link it to the document we found.
Use the same template for tomorrow.
Add these as tasks instead.
```

### Direct capability versus Work Item

Direct:

```text
Open today's daily note.
```

Work Item:

```text
Summarize this conversation, create a project note from my template,
link the three documents we discussed, synchronize it, and give me a button
to open it on the phone.
```

The latter is a playbook, not one giant tool call.

## Recommended Playbooks

### ConnectObsidianAccount

```text
issue Telegram setup session
    -> authenticate Headless account
    -> list remote vaults
    -> select or auto-select vault
    -> collect E2EE password if required
    -> create local checkout
    -> initial Sync
    -> set default logical vault
    -> report ready
```

This playbook is UI-driven and never model-driven for credentials.

### CaptureConversationToObsidian

```text
select conversation range
    -> summarize
    -> resolve logical vault and destination
    -> resolve template or capture format
    -> create or update note
    -> verify local revision
    -> verify server-side Sync
    -> return mobile open action
```

### AppendToDailyNote

```text
resolve date and time zone
    -> resolve daily note path
    -> format capture or task
    -> append idempotently
    -> verify local revision and Sync
```

### SearchAndOpenObsidianNote

```text
parse query and vault scope
    -> native vault search
    -> optional semantic search
    -> rank and deduplicate
    -> select or ask user
    -> generate exact Obsidian URI or plugin open action
```

### UpdateObsidianMetadata

```text
resolve active or named note
    -> read current properties
    -> validate typed mutations
    -> apply one atomic update
    -> synchronize
    -> return changed property set
```

### LinkFridayObjectToObsidian

```text
resolve Friday object
    -> resolve Obsidian note
    -> create durable binding
    -> update property or managed links region
    -> synchronize
    -> return both navigation targets
```

### ExportFridayResearchToObsidian

```text
resolve completed Work Item outcome
    -> choose template
    -> render claims, sources, and uncertainties
    -> create note
    -> create source links or managed region
    -> synchronize
    -> return mobile open action
```

### BuildObsidianBase

```text
interpret collection
    -> build typed BaseSpec
    -> preview if ambiguous
    -> create or update Base
    -> query and verify
    -> synchronize
```

### IngestObsidianNoteIntoFriday

```text
resolve note and revision
    -> read exact content
    -> create stable source_ref
    -> ordinary Friday ingestion
    -> return Inbox or promotion outcome
    -> bind source identity
```

## Companion event synchronization

Relevant events:

```text
installation.connected
installation.disconnected
vault.connected
vault.disconnected
note.created
note.modified
note.renamed
note.deleted
active_file.changed
selection.changed
workspace.changed
commands.changed
```

Processing:

```text
plugin emits event
    -> Friday stores event ID
    -> update installation and note binding
    -> enqueue bounded reindex or Inbox action if configured
    -> acknowledge event
```

Do not send complete note bodies in every event. Request the body only when required.

After reconnect:

```text
last acknowledged event ID
current vault manifest digest
changed paths since checkpoint when available
```

If no checkpoint is available, perform a bounded manifest comparison over configured roots.

## Suggested storage projection

```text
obsidian_account_connections
    id
    owner_id
    headless_profile_id
    account_display
    account_state
    session_epoch
    connected_at
    last_probe_at
    disconnected_at nullable

obsidian_headless_profiles
    id
    owner_id
    profile_root_ref
    headless_version
    state
    created_at
    updated_at

obsidian_setup_sessions
    id
    owner_id
    telegram_user_id
    token_digest
    state
    expires_at
    headless_process_ref nullable
    selected_remote_vault_id nullable
    created_at
    updated_at

obsidian_logical_vaults
    id
    owner_id
    account_connection_id
    display_name
    default
    configuration_json
    enabled
    created_at
    updated_at

obsidian_remote_vault_bindings
    id
    logical_vault_id
    remote_vault_id
    remote_vault_name
    shared
    region nullable
    created_at
    updated_at

obsidian_local_checkouts
    id
    logical_vault_id
    local_path_ref
    sync_mode
    sync_state
    last_successful_sync_at
    supervisor_state
    created_at
    updated_at

obsidian_installations
    id
    owner_id
    kind
    platform
    device_name
    plugin_version nullable
    obsidian_version nullable
    session_epoch
    last_seen_at

obsidian_device_vault_bindings
    id
    installation_id
    logical_vault_id
    local_vault_id nullable
    local_vault_name
    created_at
    updated_at

obsidian_note_bindings
    id
    logical_vault_id
    integration_id
    current_path
    current_revision
    ownership_mode
    friday_object_kind nullable
    friday_object_id nullable
    deleted_at nullable
    created_at
    updated_at

obsidian_operations
    id
    work_item_id nullable
    logical_vault_id
    method
    arguments_digest
    expected_revision nullable
    adapter
    status
    attempt
    local_result_json
    sync_result_json
    created_at
    updated_at

obsidian_events
    id
    installation_id
    remote_event_id
    kind
    subject_identity
    revision nullable
    status
    occurred_at
    processed_at nullable

obsidian_index_state
    logical_vault_id
    note_binding_id nullable
    path
    source_revision
    embedding_revision nullable
    indexed_at
    deleted_at nullable

obsidian_command_profiles
    installation_id
    command_id
    display_name
    effect
    requirements_json
    catalog_digest
    observed_at
```

Large note bodies and credentials do not belong in operation or setup rows.

## Friday API surface

### Telegram onboarding and account management

```text
POST   /api/obsidian/connect/start
GET    /api/obsidian/connect/{session_id}
POST   /api/obsidian/connect/{session_id}/input
POST   /api/obsidian/connect/{session_id}/select-vault
POST   /api/obsidian/connect/{session_id}/cancel

GET    /api/obsidian/account
POST   /api/obsidian/account/reauthenticate
DELETE /api/obsidian/account
```

### Vault configuration and status

```text
GET    /api/obsidian/vaults
POST   /api/obsidian/vaults
PATCH  /api/obsidian/vaults/{id}
DELETE /api/obsidian/vaults/{id}

GET    /api/obsidian/status
POST   /api/obsidian/probe
POST   /api/obsidian/vaults/{id}/sync
GET    /api/obsidian/vaults/{id}/sync-status
```

### Notes and operations

```text
GET    /api/obsidian/notes/search
GET    /api/obsidian/notes/read
POST   /api/obsidian/operations
GET    /api/obsidian/operations/{id}
```

### Companion plugin

```text
WS     /api/obsidian/plugin
POST   /api/obsidian/plugin/events
POST   /api/obsidian/plugin/ack
```

### Index

```text
POST   /api/obsidian/index/rebuild
GET    /api/obsidian/index/status
```

The conversational runtime calls the service directly through Execution Kernel handlers rather than its own HTTP API.

## Diagnostics

Add an Obsidian diagnostics section:

```text
connected accounts
Headless version and authentication state
configured logical and remote vaults
local checkouts
continuous Sync processes
last successful server-side Sync
Sync backlog and errors
available operation adapters
desktop CLI version
connected companion installations
plugin protocol version
command catalog digests
last successful operation
uncertain operations
last event checkpoint
index coverage
reconciliation backlog
```

Example:

```text
Obsidian account: connected
Headless: 0.x, authenticated
Default logical vault: Work
Remote vault: Work
Server checkout: ready
Continuous Sync: healthy
Last server Sync: 21:43
Desktop CLI: unavailable
Phone plugin: offline
Indexed roots: 2
Pending events: 0
Uncertain operations: 0
```

An offline phone plugin is not a health failure when Headless Sync is healthy.

## MCP position

MCP is not required for the internal Friday-to-Obsidian integration.

Preferred path:

```text
Friday-owned tools
    -> ObsidianService
    -> vault file adapter
    -> Headless Sync
    -> optional CLI or companion plugin
```

An optional MCP façade may later expose the same stable capabilities to external agents.

A generic third-party Obsidian MCP server should not be the primary integration because it would not understand Friday account bindings, Work Items, remote and local vault identities, operation reconciliation, projection identity, or completion semantics.

## Implementation phases

### P0: contracts and official capability probe

- freeze account, logical-vault, remote-vault, checkout, installation, and note identities;
- probe the installed Obsidian Headless contract;
- record interactive login, MFA, remote-vault listing, Sync setup, status, unlink, and logout behavior;
- probe official desktop CLI where available;
- define `ObsidianOperation`, `ObsidianOperationResult`, and error taxonomy;
- define the JOP tool-provider extension;
- define the setup-session state machine.

Deliverable: executable probes and frozen contracts.

### P1: Telegram onboarding and Headless connection

Implement:

```text
/obsidian status panel
Telegram Mini App entry
single-use HTTPS fallback
validated Telegram identity binding
per-user Headless profile
interactive ob login broker
MFA flow
remote-vault listing and selection
E2EE prompt
local checkout creation
initial Sync
continuous Sync supervisor
disconnect and reauthentication
```

Deliverable: one user can connect one remote vault from a phone and Friday keeps a server-side checkout synchronized continuously.

### P2: mobile-first vault operations

Implement against the server checkout:

```text
list and search notes
read note
create note
append and prepend
properties and tags
backlinks and outgoing links
daily note operations
template-based create
tasks
managed regions
mobile Obsidian URI generation
```

Register a small Friday-owned tool surface through Execution Kernel.

Deliverable: the user can perform the most common Obsidian work entirely through Telegram and receive one-tap mobile open links.

### P3: vault registry, note bindings, and operation ledger

- support multiple logical and remote vaults;
- add stable note bindings;
- attach revision digests to reads and writes;
- support managed, linked, and user-owned modes;
- implement managed-region merge;
- add operation ledger and reconciliation;
- add semantic rename and link-update behavior.

Deliverable: renames, moves, retries, and follow-ups no longer depend on paths alone.

### P4: Interaction Control Plane integration

- add `ObsidianActiveFrame`;
- persist candidate sets;
- implement continuation references;
- add the first Playbooks;
- return typed CapabilityOutcomes;
- use Completion Gates for local write plus Sync postconditions.

Deliverable: requests such as "open the second one" and "add that there" continue the correct task.

### P5: desktop CLI adapter

Implement:

```text
native desktop search and editing
workspaces and tabs
active file
native rename behavior
command catalog and command execution
plugin-command bridge
```

Deliverable: same-machine users receive richer native Obsidian control without changing the mobile-first core.

### P6: companion plugin

Implement:

- pairing to an existing Friday logical vault;
- outbound WebSocket;
- active note, heading, and selection context;
- atomic note processing;
- event stream;
- device-local vault identity;
- Friday command palette;
- mobile and desktop status indicator;
- reconnect and operation deduplication.

Deliverable: Friday can interact with the currently open note while Obsidian is active.

### P7: indexed mode and cross-corpus retrieval

- configure browse and index roots;
- build incremental note passage index;
- combine vault-native and semantic search;
- exclude Friday projections from re-ingestion;
- handle rename, modify, and delete events;
- expose index coverage;
- combine Obsidian notes with Friday documents and conversations.

Deliverable: Friday finds Obsidian notes by approximate content and composes them with the rest of the user's memory.

### P8: bindings, Inbox, Bases, and advanced functionality

- create durable Friday-to-Obsidian bindings;
- add Open in Friday and Open in Obsidian actions;
- support explicit note ingestion and Inbox folders;
- generate typed Base files;
- add optional custom Friday Bases view;
- profile community-plugin commands;
- evaluate Canvas separately.

### P9: optional future surfaces

- optional MCP façade;
- optional alternative Sync providers;
- optional browser-assisted account setup if Obsidian publishes a supported delegation flow;
- optional packaged plugin release and community-directory submission.

## Suggested first release scope

The first production-useful mobile slice should be:

```text
one Friday user
one Obsidian account connection
one default remote vault
Telegram /obsidian onboarding
Mini App plus HTTPS fallback
per-user Headless profile
continuous Sync
server-side note list/search/read/create/append
properties
daily notes
templates
backlinks
one CaptureConversationToObsidian playbook
one SearchAndOpenObsidianNote playbook
operation ledger
Obsidian URI button
```

Desktop CLI and the companion plugin should not block this first mobile release.

## Acceptance criteria

### Onboarding

- `/obsidian` shows a connection panel.
- An official Telegram client opens the Mini App.
- A client without Mini App support can use the single-use HTTPS link.
- Friday validates the Telegram setup identity without a separate Friday password.
- A user completes `ob login` through the setup page.
- MFA is handled when requested.
- One remote vault is selected automatically.
- Multiple remote vaults produce one compact choice.
- E2EE password is requested only when required.
- Initial Sync creates a usable server checkout.
- Refreshing the page does not duplicate the account connection.

### Account and vault identity

- Two Friday users never share one Headless profile.
- A remote vault ID, logical vault ID, server checkout, and phone local vault are distinct records.
- Changing Obsidian accounts increments `session_epoch` and invalidates stale bindings.
- A same-named vault under another account is not silently rebound.
- Disconnect stops Sync and clears Headless authentication without deleting the remote vault.

### Mobile workflow

- Friday updates a note while the phone Obsidian app is closed.
- The server-side Headless client synchronizes the update.
- Friday reports server-side Sync accurately without claiming phone delivery.
- The response includes an Obsidian URI button.
- A configured mobile vault alias opens the correct note.
- The integration remains functional while the mobile companion plugin is offline.

### Basic control

- Friday creates a note at an exact path and returns path and revision.
- Friday reads, appends, prepends, moves, renames, and opens a note.
- Friday sets and removes typed properties.
- Friday appends to today's daily note.
- Friday creates a note from a template.
- Every mutating success distinguishes local write from server Sync.

### Search and graph

- Friday searches exact text and returns paths with excerpts.
- Friday returns backlinks and outgoing links.
- An indexed note can be found by approximate semantic description.
- Native and semantic results deduplicate to one candidate.
- Friday projections are not re-ingested as independent evidence.

### Continuation

- "Open the second one" uses the active candidate set.
- "Add that there" uses the active note and Work Item outcome.
- A renamed note remains the same bound note.
- An expired candidate set is not silently reused.

### Editing and conflicts

- A managed-region update preserves user text outside the region.
- A stale full replacement returns `conflict`.
- A rename collision does not overwrite the destination.
- An uncertain mutation is reconciled before retry.
- Exactly one durable write is produced for one accepted operation.

### Desktop and plugin

- Desktop CLI and server-file adapters return the same outcome shape.
- The plugin reconnects after Friday or Obsidian restarts.
- Duplicate plugin operation IDs do not duplicate writes.
- Rename and delete events update Friday bindings.
- The current note and selection can be sent to Friday.
- Plugin offline state does not mark Headless Sync unhealthy.

### Bases, tasks, and commands

- Friday queries a supported Base and receives structured rows.
- Friday creates a supported `.base` file from a typed specification.
- Friday lists and updates a task using a durable target.
- Friday lists command IDs from an active desktop or plugin connection.
- A command without an observable postcondition remains `uncertain`.

## Suggested executable regression tests

```text
test_obsidian_connect_starts_from_a_telegram_identity.py
test_the_https_fallback_uses_one_single_use_setup_token.py
test_refreshing_the_setup_page_does_not_duplicate_login.py
test_mfa_continues_the_same_headless_login_session.py
test_one_remote_vault_is_selected_automatically.py
test_multiple_remote_vaults_require_one_explicit_choice.py
test_e2ee_password_is_requested_only_when_headless_requires_it.py
test_two_friday_users_never_share_a_headless_profile.py
test_account_switch_invalidates_stale_remote_vault_bindings.py
test_disconnect_stops_sync_and_logs_out_the_profile.py

test_friday_can_update_a_vault_while_the_phone_app_is_closed.py
test_a_successful_write_distinguishes_local_and_server_sync.py
test_friday_never_claims_mobile_delivery_without_device_evidence.py
test_the_mobile_open_link_uses_the_configured_device_vault_alias.py
test_mobile_plugin_offline_does_not_break_headless_operations.py

test_friday_can_create_and_open_an_obsidian_note.py
test_friday_can_append_to_the_daily_note_once.py
test_a_note_rename_preserves_the_integration_identity.py
test_a_managed_region_preserves_user_text.py
test_a_stale_replacement_returns_conflict.py
test_an_uncertain_append_is_reconciled_before_retry.py
test_native_and_semantic_search_results_deduplicate.py
test_a_friday_projection_is_not_reingested_as_new_knowledge.py
test_the_second_result_uses_the_active_candidate_set.py
test_add_that_there_uses_the_active_note.py

test_a_plugin_reconnect_resumes_pending_operations.py
test_duplicate_plugin_operation_ids_do_not_duplicate_writes.py
test_note_events_reindex_only_the_changed_note.py
test_delete_events_remove_search_passages.py
test_a_base_query_returns_typed_rows.py
test_a_command_without_postcondition_remains_uncertain.py
test_cli_plugin_and_file_adapters_return_the_same_outcome_shape.py
test_obsidian_tools_are_registered_by_the_organ_not_agent_runtime.py
```

## Key architectural invariants

1. Friday owns the natural-language task and Work Item.
2. Obsidian owns the remote account and native client experience.
3. Headless Sync is the persistent mobile delivery backbone.
4. The companion plugin is optional foreground context, not the backbone.
5. A browser session is not a Headless session.
6. Friday never depends on undocumented cookie transplantation for login.
7. One Friday user owns one or more isolated Headless profiles and logical vaults.
8. Remote vault, server checkout, desktop local vault, and phone local vault are separate identities.
9. The operation adapter is replaceable; the Friday capability contract is stable.
10. SQLite remains the source of truth for Friday objects.
11. Obsidian remains the source of truth for user-owned Obsidian notes.
12. A managed projection is rebuildable and never treated as independent evidence.
13. A linked note may contain user and Friday-owned regions without silent overwrite.
14. Path is navigation, not durable identity after binding.
15. Local write, server Sync, and phone delivery are separate postconditions.
16. Tool success is not task completion.
17. Raw CLI, Headless, or plugin prose never becomes model-visible evidence without normalization.
18. One accepted operation produces at most one durable mutation.
19. The model may select and parameterize capabilities, but it never handles login secrets or writes vault files directly.

## Final recommendation

Build the integration in this order:

```text
Telegram onboarding
    -> per-user Headless account connection
    -> remote vault selection
    -> continuous server-side Sync
    -> core vault operations
    -> Friday-owned conversational tools
    -> operation ledger and note identity
    -> Work Items and Playbooks
    -> desktop CLI
    -> companion plugin
    -> semantic indexing and advanced Obsidian features
```

The existing `MemoryVault` remains a simple, rebuildable Friday projection. The new Obsidian Organ provides interactive access to ordinary user vaults and to the always-on server checkout.

The intended mobile experience is:

```text
The user sends /obsidian once.
The user completes one compact Obsidian login flow.
Friday keeps the selected remote vault synchronized continuously.
The user commands Friday in ordinary Russian from Telegram.
Friday edits, searches, and organizes Obsidian notes.
The phone receives changes through Obsidian Sync.
Friday returns one-tap links that open the result in the official mobile app.
```

This is the useful integration boundary. Friday becomes a semantic and conversational control surface for Obsidian, while Obsidian remains the mature human workspace rather than a feature list Friday must rebuild.

## Official references

Sources checked on 20 August 2026:

- [Obsidian Headless](https://obsidian.md/help/headless)
- [Headless Sync](https://obsidian.md/help/sync/headless)
- [Obsidian CLI](https://obsidian.md/help/cli)
- [Obsidian URI](https://help.obsidian.md/Extending%2BObsidian/Obsidian%2BURI)
- [Obsidian Vault developer guide](https://docs.obsidian.md/Plugins/Vault)
- [Build an Obsidian plugin](https://docs.obsidian.md/Plugins/Getting%20started/Build%20a%20plugin)
- [Mobile plugin development](https://docs.obsidian.md/Plugins/Getting%20started/Mobile%20development)
- [Build a Bases view](https://docs.obsidian.md/plugins/guides/bases-view)
- [Telegram Mini Apps](https://core.telegram.org/bots/webapps)
