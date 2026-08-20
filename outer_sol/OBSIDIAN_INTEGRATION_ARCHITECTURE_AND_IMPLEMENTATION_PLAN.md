# Friday Obsidian Integration Architecture and Implementation Plan

> Document ID: FRIDAY-OBS-001  
> Status: External architecture proposal, draft v0.1  
> Repository snapshot: `main`, Friday `0.206.0`, 20 August 2026  
> Scope: user-facing Obsidian control through Friday, vault and note identity, CLI and companion-plugin transports, note search and editing, properties, links, tasks, templates, Bases, workspace actions, event synchronization, operational-memory integration, implementation phases, and acceptance criteria.  
> Related documents: [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), [`MCP_ARCHITECTURE_OBSERVATION.md`](MCP_ARCHITECTURE_OBSERVATION.md), and [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md).

## Scope decision

This proposal is intentionally functionality-first.

It assumes that enabling Obsidian integration is an explicit deployment and user choice. It does not redesign the sensitive-document boundary, local application trust model, encryption, or restricted-data policy covered by the other architecture documents.

Existing Friday authorization, actor scope, effect classification, idempotency, audit, and publication contracts should remain in place. Beyond that baseline, the goal here is straightforward:

> A person should be able to use useful Obsidian functionality by talking to Friday, while also being able to invoke Friday from inside Obsidian when that improves the workflow.

The integration should feel like one workspace with two interfaces:

```text
Friday
    natural-language intent
    semantic retrieval
    document and conversation context
    multi-step coordination
    model synthesis

Obsidian
    notes and folders
    links and backlinks
    properties and tags
    tasks and daily notes
    templates
    Bases
    tabs, workspaces, and native editing
```

Friday should not attempt to reproduce Obsidian. Obsidian should not become a second implementation of Friday's memory, provenance, or work-state engine.

## Executive recommendation

Build the integration as a first-party Friday Organ plus a small Obsidian companion plugin.

Use two transports behind one Friday-owned capability contract:

```text
1. Obsidian CLI transport
   - fastest path to a useful desktop MVP
   - broad access to current native Obsidian commands
   - no plugin required for basic operations

2. Friday companion plugin transport
   - event-driven connection
   - active note, selection, tabs, and workspace context
   - atomic in-app modifications
   - rename and delete notifications
   - richer command execution
   - works when Friday cannot execute the desktop CLI directly
```

Add Obsidian URI as a small navigation fallback for opening notes, creating notes, daily notes, and opening search.

Keep direct filesystem access limited to bulk projection and import jobs. It should not be the primary interactive command transport.

The target architecture is:

```text
User message
    -> Interaction Control Plane
    -> Obsidian Playbook or direct capability
    -> Friday Obsidian Service
    -> transport selection
         -> CLI transport
         -> companion plugin transport
         -> URI navigation fallback
    -> typed CapabilityOutcome
    -> Work Item continuation and Completion Gate
    -> one user-visible response
```

## Why the current Friday code is a useful starting point

Friday already has several relevant foundations.

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

It is not sufficient as the interactive integration because its README explicitly states that edits will be overwritten on the next synchronization. A user-facing Obsidian integration needs writable notes, note identity across renames, conflict handling, command execution, active-editor context, and incremental events.

### Friday Organ Protocol

[`docs/ORGANS.md`](../docs/ORGANS.md) already defines code-owned extension modules with capabilities, workers, and routers. Obsidian is a good Organ candidate because it is optional, contributes an API surface, may run a connection/reconciliation worker, and should not enlarge the central agent runtime.

### Execution and orchestration boundaries

Friday already has:

- capability-gated tools;
- execution-kernel ownership of effects;
- V12 typed planning contracts;
- durable missions;
- the proposed Interaction Control Plane and Work Items;
- document, conversation, graph, and generated-file services.

The Obsidian integration should attach to these boundaries rather than build a separate agent loop.

## Product experience

The first release should support requests such as:

```text
Create a note in Projects/Friday from this conversation.

Append this summary to today's daily note.

Find my Obsidian notes about the document registry and open the best match.

Show what links to the note called Friday Architecture.

Add status=review and project=Friday to that note.

Move the second result to Archive/2026.

Create a task in today's note to review the retrieval audit tomorrow.

Use the Meeting template and create a note for Friday architecture review.

Build a Base for notes tagged project/friday with status not equal to done.

Link this Friday document to the Obsidian note we opened earlier.

Save the research result in Obsidian, then add its source links below it.

Open the note in a split pane.

Load my Research workspace.
```

Inside Obsidian, the companion plugin should eventually support commands such as:

```text
Ask Friday about the current note
Send the current selection to Friday
Find related Friday documents
Find related Obsidian notes using Friday semantic search
Save the current Friday answer here
Create a Friday-managed summary block
Refresh a managed block
Ingest the current note into Friday Inbox
Open the linked Friday source
Continue the active Friday Work Item
```

## Non-goals

The integration should not initially attempt to:

- replace Obsidian Sync;
- build a competing Markdown editor;
- reproduce the complete Obsidian plugin ecosystem inside Friday;
- make Obsidian the canonical database for Friday;
- make every Friday object a writable Markdown file;
- expose arbitrary filesystem access to the model;
- index every vault automatically without user configuration;
- mirror every conversation message as a note;
- support Canvas editing in the first release;
- promise desktop and mobile parity in the first release;
- use MCP as a required internal hop.

## Integration modes

One global bidirectional-sync switch is too blunt. The integration should expose explicit modes per vault and per folder.

### 1. Control mode

Friday invokes Obsidian features but does not continuously ingest the vault.

Examples:

- create or open notes;
- append to daily notes;
- change properties;
- query backlinks;
- run a Base query;
- load a workspace.

This is the best MVP mode.

### 2. Browse mode

Friday may list, search, read, and navigate selected vault folders without converting notes into Knowledge Objects.

Obsidian remains the note store. Friday may use the note content for the active task.

### 3. Indexed mode

Friday incrementally indexes selected Obsidian notes for semantic retrieval.

The index is a rebuildable external-source projection. Indexing does not silently promote notes into canonical Friday knowledge.

### 4. Inbox mode

A configured Obsidian folder acts as an explicit intake queue.

A note placed in that folder may enter Friday's ordinary Raw Object and Inbox review pipeline with a stable source reference.

### 5. Managed projection mode

Friday writes complete generated notes or projections into a configured subtree.

Those notes are regenerated from Friday state and are not user-authoritative.

The current `MemoryVault` is conceptually this mode.

### 6. Linked-note mode

A user-owned Obsidian note is associated with a Friday object. Friday may update selected properties or marked content regions while preserving the rest of the note.

This is the preferred long-term mode for collaboration between the two systems.

## Recommended repository structure

Implement the server-side integration as an Organ:

```text
friday/organs/obsidian/
    __init__.py
    contracts.py
    models.py
    service.py
    router.py
    worker.py
    cli_transport.py
    plugin_transport.py
    uri_transport.py
    note_identity.py
    note_merge.py
    indexer.py
    tools.py
    playbooks.py
    diagnostics.py
```

Implement the companion plugin in a separate top-level package or repository:

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
        protocol.ts
        commands.ts
        events.ts
        note-operations.ts
        friday-view.ts
        status-bar.ts
```

Keeping the TypeScript plugin separate from the Python package makes build and release boundaries clear. It may remain in the same monorepo if that is operationally easier.

## JOP extension required for natural-language tools

The current Friday Organ Protocol exposes capabilities, workers, and HTTP routers, but not a first-class tool-provider extension.

Obsidian is a strong justification for a small JOP extension rather than wiring many tool handlers into the legacy runtime.

Proposed extension:

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

A `ToolRegistration` should contain a Friday-owned `ToolSpec`, a handler, an effect class, and the required capability.

The Organ registry then contributes those registrations to the existing Execution Kernel during startup.

This extension will also benefit future SaaS and MCP-backed Organs. It should remain code-owned and explicitly registered, preserving JOP's no-dynamic-discovery rule.

If the architect prefers not to change JOP in the first release, the Obsidian service may be registered as a core tool provider temporarily, but the integration must still remain outside `AgentRuntime` branching.

## Transport abstraction

All user-visible behavior should call one interface regardless of how Obsidian is reached.

```python
class ObsidianTransport(Protocol):
    async def probe(self) -> ObsidianTransportStatus: ...

    async def execute(
        self,
        operation: ObsidianOperation,
        *,
        absolute_deadline: float,
    ) -> ObsidianOperationResult: ...

    async def reconcile(
        self,
        operation_id: str,
        *,
        absolute_deadline: float,
    ) -> ObsidianOperationResult | None: ...
```

The service chooses a transport from the registered vault connection:

```text
plugin connection available
    -> companion plugin transport

same-host desktop and CLI available
    -> CLI transport

navigation-only operation
    -> Obsidian URI fallback

bulk managed projection
    -> filesystem projector
```

Transport choice must not change the Friday capability contract returned to the model.

## Obsidian CLI transport

The official Obsidian CLI provides a broad functional surface suitable for the first release. It can target vaults, notes, daily notes, properties, tags, tasks, links, Bases, templates, commands, plugins, themes, workspaces, tabs, and search.

### Why it is the preferred MVP

- no custom plugin is required for common operations;
- it already understands Obsidian's link resolution and note paths;
- move and rename operations use Obsidian behavior, including link updates when enabled;
- many read operations provide JSON, CSV, TSV, or Markdown output;
- arbitrary registered Obsidian commands can be invoked by command ID;
- it can open the resulting file in the app.

### CLI execution rules

The adapter should:

- invoke the binary without a shell;
- pass every argument as a separate subprocess argument;
- use `vault=<id>` as the first parameter;
- prefer exact `path=` over ambiguous `file=` after a note has been resolved;
- request `format=json` where supported;
- enforce a per-operation deadline;
- cap stdout and stderr;
- return a typed status instead of exposing raw CLI prose to the model;
- probe `obsidian version`, `obsidian vaults verbose`, and `obsidian commands`;
- cache a capability manifest by Obsidian version;
- mark the transport unavailable when the command contract does not match the expected version.

Illustrative adapter calls:

```text
obsidian vault=<id> search query=<query> format=json
obsidian vault=<id> read path=<path>
obsidian vault=<id> create path=<path> content=<content> open
obsidian vault=<id> append path=<path> content=<content>
obsidian vault=<id> property:set path=<path> name=<name> value=<value> type=<type>
obsidian vault=<id> backlinks path=<path> format=json
obsidian vault=<id> base:query path=<base-path> view=<view> format=json
obsidian vault=<id> command id=<command-id>
obsidian vault=<id> workspace:load name=<workspace>
```

### CLI limitation

The CLI connects to the desktop application. It is ideal when Friday runs in the same desktop user environment. It is less convenient when Friday runs in Docker, on another host, or under a service account without access to the desktop session.

That limitation is the main reason to add the companion plugin rather than stretching the CLI adapter into every topology.

## Companion plugin transport

The plugin should open an outbound WebSocket connection to Friday. Friday should not assume that it can connect into the Obsidian process.

```text
Obsidian companion plugin
    -> authenticated local WebSocket
    -> Friday Obsidian Organ router
```

An outbound plugin connection works for:

- Friday running in a container;
- Friday running under a different local service account;
- Friday running on another reachable machine;
- future mobile support, subject to platform and connectivity constraints.

### Handshake

```json
{
  "protocol": "friday.obsidian.v1",
  "plugin_version": "0.1.0",
  "obsidian_version": "1.12.7",
  "vault": {
    "id": "ef6ca3e3b524d22f",
    "name": "Notes"
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

Friday records the live session but does not use the session ID as durable vault identity.

### Request envelope

```json
{
  "protocol": "friday.obsidian.v1",
  "operation_id": "obsop_...",
  "vault_id": "ef6ca3e3b524d22f",
  "method": "note.process",
  "arguments": {
    "path": "Projects/Friday.md",
    "edit": {
      "kind": "managed_block_replace",
      "block_id": "friday-summary",
      "content": "..."
    }
  },
  "expected_revision": "sha256:...",
  "deadline_ms": 10000
}
```

### Response envelope

```json
{
  "operation_id": "obsop_...",
  "status": "success",
  "result": {
    "path": "Projects/Friday.md",
    "revision": "sha256:...",
    "changed": true
  },
  "changed_paths": ["Projects/Friday.md"]
}
```

### Event envelope

```json
{
  "protocol": "friday.obsidian.v1",
  "event_id": "obsevt_...",
  "vault_id": "ef6ca3e3b524d22f",
  "kind": "note.renamed",
  "occurred_at": "2026-08-20T18:30:00+02:00",
  "payload": {
    "old_path": "Projects/Friday.md",
    "new_path": "Projects/Friday Architecture.md",
    "integration_id": "obnote_...",
    "revision": "sha256:..."
  }
}
```

### Plugin responsibilities

The plugin should:

- use Obsidian's `Vault` APIs for note operations;
- use `Vault.process()` for read-modify-write operations;
- listen for create, modify, rename, and delete events;
- expose the current file, selection, active leaf, and workspace state;
- list available command IDs;
- execute explicit command IDs;
- register Friday-facing command-palette actions;
- maintain a small reconnecting outbound session;
- deduplicate operation IDs;
- acknowledge events only after Friday accepts them;
- avoid storing Friday's work state inside the note body.

## Obsidian URI fallback

Obsidian URI is useful for navigation-only actions:

```text
open a vault
open a note
create or append to a note
open or create the daily note
open search with a query
```

The URI fallback should be used when:

- the user explicitly asks Friday to open something in Obsidian;
- the CLI is not registered but desktop URI handling is available;
- no result data needs to be returned to Friday.

URI invocation should not be treated as proof that a write completed. If an operation needs a postcondition, use CLI or plugin transport.

## Vault registry

Friday needs a durable vault registry independent of transport sessions.

```python
@dataclass(frozen=True, slots=True)
class ObsidianVault:
    id: str
    owner_id: str
    obsidian_vault_id: str
    display_name: str
    local_path_hint: str | None
    preferred_transport: str
    default_capture_folder: str
    daily_note_enabled: bool
    browse_roots: tuple[str, ...]
    index_roots: tuple[str, ...]
    inbox_roots: tuple[str, ...]
    managed_roots: tuple[str, ...]
    enabled: bool
```

The Obsidian vault ID is preferable to the display name because names may collide or change.

The physical path is a deployment hint, not the primary identity. Plugin transport does not require Friday to know the path.

### Multiple vaults

Friday should support:

- one default vault per user;
- explicit named vault selection;
- a remembered active vault inside the current Work Item;
- per-vault folder defaults;
- different integration modes per vault.

Natural-language examples:

```text
Save this to my Work vault.

Search the Personal vault instead.

Use this vault as the default for daily notes.
```

## Note identity

Obsidian naturally addresses notes by vault-relative path. Paths change when users rename or move notes, so Friday needs a stable integration identity.

### Managed and linked notes

Friday should add a property:

```yaml
friday_obsidian_id: obnote_7d18d2f4c9e44a35
```

Optional related properties:

```yaml
friday_object_id: ko_...
friday_raw_object_id: raw_...
friday_projection_kind: linked
friday_projection_revision: 4
```

The plugin keeps the integration ID stable across move and rename events.

### Unmodified user notes

Friday must not require adding a property merely to read or search a note.

For an unbound note, the temporary identity is:

```text
vault_id + exact path + observed revision
```

A durable binding is created only when:

- the user asks Friday to link the note;
- Friday creates the note;
- Friday creates a managed region;
- indexed mode is configured to assign IDs.

### Identity invariants

- note title is not identity;
- path is not permanent identity after binding;
- content digest is a revision, not identity;
- a copy of a note receives a new integration ID;
- rename and move update the binding without creating a new note;
- deletion tombstones the binding;
- restore or recreation is reconciled explicitly.

## Note ownership and editing modes

Each note binding should declare how Friday may modify it.

### User-owned

The note is entirely user-authored.

Friday may read it and may perform an explicit full-note edit requested by the user. It does not update it automatically.

### Linked

The note is user-owned, but Friday may modify selected properties and explicitly marked managed regions.

### Friday-managed

Friday owns the full note body. It may regenerate the note from a Friday object or workflow result.

### Projection

The note is a rebuildable mirror such as the current `MemoryVault` output. Edits are not imported.

### Inbox note

The note is user-owned and selected for import into Friday's Inbox. Subsequent editing behavior is configured separately.

## Managed regions

For linked notes, Friday should update marked regions rather than rewriting the complete file.

```markdown
<!-- friday:managed:start id="summary" revision="4" -->
## Friday summary

Generated content here.
<!-- friday:managed:end id="summary" -->
```

Rules:

- region IDs are unique within a note;
- Friday updates only the selected region;
- content outside managed markers is preserved byte-for-byte where practical;
- a malformed or duplicate marker returns `ambiguous`, not a best-effort rewrite;
- the plugin uses `Vault.process()` to apply the update to the latest content;
- each successful write returns a new revision digest.

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

Even in a functionality-first integration, silent loss of user edits is unacceptable.

Every read result should include:

```text
vault_id
path
integration_id if present
revision digest
modified time
```

Every destructive replacement should accept `expected_revision`.

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
uncertain
failed
```

### Conflict policy

Default behavior:

```text
append/prepend
    -> apply to current note through plugin or CLI

property update
    -> apply to current frontmatter

managed region update
    -> merge into current body

full replacement with stale revision
    -> return conflict

rename target already exists
    -> return conflict

delete after uncertain transport failure
    -> reconcile before retry
```

The user may opt into an explicit overwrite request, but the model must not silently convert a normal edit into overwrite.

## Friday-owned capability surface

Do not publish the full CLI command catalog directly to the model. Expose stable Friday capabilities whose handlers may use CLI or plugin transport.

### Discovery and navigation

```text
obsidian_list_vaults
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

### Expert command bridge

```text
obsidian_list_commands
obsidian_run_command
```

`obsidian_run_command` should require an exact command ID returned by the current Obsidian command catalog. The user may request third-party plugin commands through this bridge without Friday needing a custom adapter for every plugin.

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
delete, permanent delete, full overwrite, plugin install/uninstall, publish, sync restore, arbitrary expert command without a known behavior profile
```

This classification is useful for predictable user interaction even when security hardening is out of scope.

## Capability contracts and typed outcomes

Each capability should return a typed `CapabilityOutcome`, as proposed in the Interaction Control Plane document.

Example search result:

```python
CapabilityOutcome(
    status="success",
    result_type="obsidian.note_candidates.v1",
    items=(
        ObsidianNoteCandidate(
            vault_id="vault_...",
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
        sources=("obsidian_cli_search", "friday_semantic_index"),
    ),
)
```

Example write result:

```python
CapabilityOutcome(
    status="success",
    result_type="obsidian.note_write.v1",
    postcondition={
        "vault_id": "vault_...",
        "path": "Projects/Friday.md",
        "revision": "sha256:...",
        "operation": "append",
    },
)
```

The final model receives the outcome, not raw CLI stdout, plugin exceptions, or filesystem traces.

## Search architecture

Friday and Obsidian have different strengths. The integration should combine them rather than choose one search engine.

### Obsidian-native search lanes

Use Obsidian for:

- exact and lexical text search;
- path and folder filtering;
- property queries;
- tags;
- tasks;
- backlinks and outgoing links;
- unresolved links;
- orphan and dead-end reports;
- Bases queries.

### Friday search lanes

Use Friday for:

- semantic search over configured indexed roots;
- approximate content;
- approximate dates after temporal parsing;
- cross-corpus search with documents and conversations;
- entity and project resolution;
- reranking;
- continuation from an active Work Item.

### Unified Obsidian result

```text
query
    -> native Obsidian search
    -> optional Friday semantic index
    -> deduplicate by vault and integration identity/path
    -> rank fusion
    -> typed candidate set
```

The result should explain its match channels:

```text
exact title
path
text
property
tag
backlink
semantic passage
recently active
```

### Avoiding duplicate Friday material

The current `MemoryVault` projection and any Friday-managed export roots should be excluded from automatic re-ingestion by default.

Otherwise:

```text
Friday Knowledge Object
    -> Markdown projection
    -> Obsidian indexing
    -> Friday imports projection
    -> duplicate source and evidence lineage
```

Projection notes may still be searched and opened through Obsidian. They should be marked with source kind `friday_projection` so Friday does not treat them as independent knowledge.

## Obsidian note indexing in Friday

Indexed mode should create a rebuildable source projection, not a Knowledge Object.

Illustrative index record:

```text
obsidian_note_index
    vault_id
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

The source reference should be stable:

```text
obsidian:<vault-id>:<integration-id>
```

or, before binding:

```text
obsidian:<vault-id>:path:<normalized-path>
```

On rename, plugin events update the path while preserving integration identity.

On content change, only the changed note is re-indexed.

On delete, passages and embeddings are invalidated.

### Browse versus ingest

Finding and reading an Obsidian note for the current Work Item does not make it canonical Friday knowledge.

Explicit import paths:

```text
"Remember this note"
"Send this note to Friday Inbox"
move note into configured Friday Inbox folder
Obsidian command: Ingest current note into Friday
```

All of these use the ordinary ingestion and review pipeline.

## Friday document and Obsidian note links

The integration should support durable links between:

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

Illustrative binding:

```python
ObsidianBinding(
    id="obsbind_...",
    vault_id="vault_...",
    note_id="obnote_...",
    note_path="Projects/Friday.md",
    subpath="#Retrieval",
    friday_object_kind="knowledge_object",
    friday_object_id="ko_...",
    relation="describes",
)
```

Useful relations:

```text
describes
annotates
summarizes
source_for
related_to
result_of_work_item
projects
```

The plugin may render an "Open in Friday" command. Friday may render an `obsidian://open` navigation action.

## Daily notes

Daily notes are one of the highest-value early integrations.

Friday should support:

```text
open today's daily note
read today's daily note
append a capture
prepend a briefing
add a task
add a completed-work summary
create or open a note for a specific date when supported by configured templates
```

Recommended capture format:

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

Friday should be able to:

- list templates;
- read a template with resolved variables where supported;
- create a note from a selected template;
- fill explicitly named placeholders;
- preserve unknown template syntax;
- return `needs_input` when required fields are missing.

A template action should be represented as a Work Item when Friday must first gather content, resolve a destination, and then create the note.

Example:

```text
Create a meeting note from my Architecture Review template using what we discussed.
```

Playbook:

```text
summarize current discussion
    -> list and resolve template
    -> extract template fields
    -> ask only for missing required fields
    -> create note
    -> verify path and revision
    -> open in Obsidian
```

## Properties and tags

Use Obsidian properties as typed fields, not as an unstructured YAML patch.

Friday operations should accept:

```text
text
list
number
checkbox
date
datetime
```

Examples:

```text
Set status to review.
Add Friday and retrieval to project tags.
Remove the obsolete property.
Set due to next Monday.
```

The service should normalize property operations into:

```python
PropertyMutation(
    name="status",
    action="set",
    value="review",
    value_type="text",
)
```

A multi-property update should be one atomic plugin operation where possible.

## Links and backlinks

Friday should expose links as structured data:

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

For links to Friday objects that do not have an Obsidian note, Friday may:

- create a managed projection note;
- link to a local Friday URL;
- leave an unresolved wikilink intentionally;
- ask the user which representation is preferred.

## Tasks

The official CLI supports listing and updating tasks. The companion plugin can provide richer context and atomic edits.

Friday should support:

```text
list incomplete tasks
list tasks in the active note or daily note
create a task by appending Markdown
mark a task done or todo
set a custom task status
open the task's note and line
```

A task reference must include:

```text
vault_id
path
line or stable block identifier
observed revision
text excerpt
status
```

Line numbers alone are unstable. The plugin should prefer block IDs when available and reconcile by exact task text plus nearby context otherwise.

## Bases

Bases should be treated as a first-class Obsidian feature, not merely as a Markdown file with an unfamiliar extension.

### Phase 1

Use native `.base` files and CLI commands:

```text
list Bases
list views
query a view
create a note through a Base
```

Friday may also generate a `.base` file from a typed specification:

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

### Phase 2

The companion plugin may register a custom Bases view for Friday-linked data.

Possible views:

```text
Friday documents linked to notes
Friday Work Items captured in Obsidian
Notes awaiting Friday ingestion
Related documents and conversations
Friday semantic matches for the selected Base row
```

The first useful release does not require a custom Bases view. Native Base generation and queries already provide substantial value.

## Workspace and UI actions

Obsidian CLI can list tabs, open tabs, save and load workspaces, and open notes. The plugin can provide more precise pane and leaf control.

Friday should eventually support:

```text
open in current tab
open in new tab
open in split
open in new window where supported
load workspace
save current workspace
list recent notes
focus Friday companion view
```

These actions are UI effects. A successful request should return the target and action, but Friday should not claim that the user read the note merely because it was opened.

## Expert command bridge

Obsidian's command palette includes commands from core and community plugins. A generic command bridge gives Friday access to functionality it does not know in advance.

Safe functional contract:

```text
1. list current command IDs and names
2. resolve a command by exact ID or unambiguous display name
3. record the current command-catalog digest
4. execute one command
5. collect observable postconditions when available
```

Command profiles may be learned locally:

```python
ObsidianCommandProfile(
    command_id="some-plugin:command",
    display_name="Some Plugin: Do thing",
    effect="mutate",
    requires_active_file=True,
    supports_postcondition=False,
)
```

Unknown commands remain expert operations. Friday should report completion as `uncertain` when the command provides no observable result.

## Interaction Control Plane integration

The Obsidian integration will be much more useful when it uses Work Items and Active Frames from [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md).

### Active Obsidian frame

```python
ObsidianActiveFrame(
    vault_id="vault_...",
    active_note_id="obnote_...",
    active_path="Projects/Friday.md",
    active_revision="sha256:...",
    active_heading="#Retrieval",
    selected_candidate_set_id="candset_...",
    last_operation_id="obsop_...",
)
```

This allows follow-ups such as:

```text
Add that there.
Open the second one.
Move it to Archive.
Now link it to the document we found.
Use the same template for tomorrow.
Add these as tasks instead.
```

The model resolves the reference, but the Work Item validates the target against durable candidate and note identities.

### Direct capability versus Work Item

Direct operation:

```text
Open today's daily note.
```

Work Item:

```text
Summarize this conversation, create a project note from my template,
link the three documents we discussed, and open it in a split pane.
```

The latter should not be one giant tool call. It should be a bounded playbook with typed outcomes.

## Recommended Playbooks

### CaptureConversationToObsidian

```text
select conversation range
    -> summarize
    -> resolve vault and destination
    -> resolve template or capture format
    -> create or update note
    -> verify revision
    -> optionally open note
```

### AppendToDailyNote

```text
resolve date
    -> resolve daily note
    -> format capture or task
    -> append
    -> verify content revision
```

### SearchAndOpenObsidianNote

```text
parse query and vault scope
    -> native search
    -> optional semantic search
    -> rank and deduplicate
    -> select or ask user
    -> open exact path
```

### UpdateObsidianMetadata

```text
resolve active or named note
    -> read current properties
    -> validate typed mutations
    -> apply one atomic update
    -> return changed property set
```

### LinkFridayObjectToObsidian

```text
resolve Friday object
    -> resolve Obsidian note
    -> create durable binding
    -> update note property or managed links region
    -> return both navigation targets
```

### ExportFridayResearchToObsidian

```text
resolve completed Work Item outcome
    -> choose template
    -> render claims, sources, and uncertainties
    -> create note
    -> create source links or managed region
    -> verify
```

### BuildObsidianBase

```text
interpret requested collection
    -> construct typed BaseSpec
    -> preview filters and columns if ambiguous
    -> create or update .base
    -> query and verify
    -> open Base
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

## Operation durability and recovery

A user may close Obsidian, restart Friday, rename a note, or lose the plugin connection during an operation.

Store each mutating operation before dispatch:

```text
operation_id
work_item_id
vault_id
method
arguments digest
expected revision
transport
status
attempt
result revision
created_at
updated_at
```

State machine:

```text
prepared
    -> dispatched
    -> succeeded
    -> failed
    -> uncertain
    -> reconciled
    -> cancelled
```

Retry rules:

- read operations may retry after transport failure;
- create operations use operation ID or target existence reconciliation;
- append operations require deduplication markers or postcondition checks;
- move and rename check source and destination;
- delete never blindly retries after an uncertain outcome;
- generic commands without postconditions remain `uncertain` after disconnect.

## Event synchronization

The companion plugin should emit incremental events instead of forcing Friday to rescan a vault continuously.

Relevant events:

```text
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

### Event processing

```text
plugin emits event
    -> Friday stores event ID
    -> update note binding
    -> enqueue bounded reindex or Inbox action if configured
    -> acknowledge event
```

Debounce repeated `note.modified` events for the same path and revision window.

Do not send complete note bodies in every event. The event identifies the changed note; Friday requests the body only when a configured mode requires it.

### Offline reconciliation

After reconnect, the plugin sends:

```text
last acknowledged event ID
current vault manifest digest
changed paths since checkpoint when available
```

If the checkpoint is unavailable, Friday performs a bounded manifest comparison over configured roots.

## Suggested storage projection

```text
obsidian_vaults
    id
    owner_id
    obsidian_vault_id
    display_name
    local_path_hint
    preferred_transport
    configuration_json
    enabled
    created_at
    updated_at

obsidian_note_bindings
    id
    vault_id
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
    vault_id
    method
    arguments_digest
    expected_revision nullable
    transport
    status
    attempt
    result_json
    created_at
    updated_at

obsidian_events
    id
    vault_id
    remote_event_id
    kind
    subject_identity
    revision nullable
    status
    occurred_at
    processed_at nullable

obsidian_index_state
    vault_id
    note_binding_id nullable
    path
    source_revision
    embedding_revision nullable
    indexed_at
    deleted_at nullable

obsidian_command_profiles
    vault_id
    command_id
    display_name
    effect
    requirements_json
    catalog_digest
    observed_at
```

Large note bodies should remain in Obsidian or Friday's existing source storage, not inside operation rows.

## Friday API surface

Suggested HTTP endpoints for configuration and the companion plugin:

```text
GET    /api/obsidian/vaults
POST   /api/obsidian/vaults
PATCH  /api/obsidian/vaults/{id}
DELETE /api/obsidian/vaults/{id}

GET    /api/obsidian/status
POST   /api/obsidian/probe

GET    /api/obsidian/notes/search
GET    /api/obsidian/notes/read
POST   /api/obsidian/operations
GET    /api/obsidian/operations/{id}

WS     /api/obsidian/plugin
POST   /api/obsidian/plugin/events
POST   /api/obsidian/plugin/ack

POST   /api/obsidian/index/rebuild
GET    /api/obsidian/index/status
```

The conversational runtime should use the service directly through Execution Kernel handlers, not call its own HTTP API.

## Diagnostics

Add an Obsidian diagnostics section:

```text
configured vaults
available transports
CLI version
Obsidian version
plugin version
plugin protocol version
connected sessions
command catalog digest
last successful operation
uncertain operations
last event checkpoint
index coverage
reconciliation backlog
```

Useful doctor output:

```text
Obsidian CLI registered: yes
Desktop reachable: yes
Companion plugin connected: yes
Default vault: Work
Configured indexed roots: 2
Pending note events: 0
Uncertain operations: 0
```

## MCP position

MCP is not required for the internal Friday-to-Obsidian integration.

The preferred internal path is:

```text
Friday-owned tools
    -> ObsidianService
    -> CLI or companion plugin
```

An optional MCP façade may be added later so external agents can invoke Friday's stable Obsidian capabilities. It should wrap the same service and contracts rather than introduce a second implementation.

A generic third-party Obsidian MCP server should not be the primary integration because it would not understand Friday Work Items, note bindings, projection identity, ingestion state, or completion semantics.

## Implementation phases

### P0: contract and topology inventory

- confirm supported Friday deployment topologies;
- probe the current Obsidian CLI on Linux;
- record exact structured output for required commands;
- define `ObsidianOperation`, `ObsidianOperationResult`, and error taxonomy;
- decide the initial JOP tool-provider extension;
- add configuration for one default vault.

Deliverable: executable CLI probe and frozen operation contracts.

### P1: CLI MVP

Implement:

```text
list vaults
search notes
read note
create note
append and prepend
open note
open search
read and set properties
backlinks and outgoing links
daily note operations
template-based create
list and update tasks
Base query
workspace load
```

Register a small Friday-owned tool surface through Execution Kernel.

Deliverable: the user can perform the most common Obsidian work through Friday without a plugin.

### P2: vault registry and note bindings

- add durable vault configuration;
- add stable note bindings;
- attach revision digests to reads and writes;
- support managed, linked, and user-owned modes;
- implement managed-region merge;
- add operation ledger and reconciliation.

Deliverable: renames, moves, retries, and follow-ups no longer depend on remembered paths alone.

### P3: companion plugin

Implement:

- outbound WebSocket connection;
- protocol handshake;
- note read/create/process/rename/delete;
- active note and selection context;
- event stream;
- Friday command palette;
- status indicator;
- reconnect and operation deduplication.

Deliverable: container and remote Friday deployments can control the desktop app, and Friday can react to Obsidian changes incrementally.

### P4: Interaction Control Plane integration

- add `ObsidianActiveFrame`;
- persist candidate sets;
- implement continuation references;
- add the first Playbooks;
- return typed CapabilityOutcomes;
- use Completion Gates for multi-step writes.

Deliverable: requests such as "open the second one" and "add that there" continue the correct task.

### P5: indexed mode

- configure browse and index roots;
- build incremental note passage index;
- combine native and semantic search;
- exclude Friday projections from re-ingestion;
- handle rename, modify, and delete events;
- expose index coverage.

Deliverable: Friday can find Obsidian notes by approximate content and combine them with document and conversation search.

### P6: links, Inbox, and Friday object binding

- create durable Friday-to-Obsidian bindings;
- add "Open in Friday" and "Open in Obsidian" actions;
- support explicit note ingestion;
- support an Obsidian Inbox folder;
- generate or refresh related-material regions.

Deliverable: notes and Friday sources become navigable parts of one knowledge workspace without merging their authority models.

### P7: Bases and advanced native functionality

- generate typed `.base` files;
- create and query Base views;
- add optional custom Friday Bases view;
- profile community-plugin command IDs;
- add workspace and pane actions;
- evaluate Canvas operations separately.

Deliverable: the integration exposes broader Obsidian functionality without adding one custom Friday adapter per plugin.

### P8: optional external surfaces

- optional MCP façade;
- optional Headless deployment adapter;
- optional mobile companion support;
- optional packaged plugin release and community-directory submission.

These are not prerequisites for the core desktop integration.

## Suggested first release scope

The first production-useful slice should be deliberately smaller than the complete plan:

```text
one user
one default vault
CLI transport
search/read/create/append/open
properties
daily notes
templates
backlinks
one CaptureConversationToObsidian playbook
one SearchAndOpenObsidianNote playbook
operation ledger
exact-path follow-ups
```

The companion plugin should be the next slice, not a prerequisite for proving the user workflow.

## Acceptance criteria

### Basic control

- Friday lists configured vaults and selects the user's default vault.
- Friday creates a note at an exact path and returns the final path and revision.
- Friday reads, appends, prepends, moves, renames, and opens a note.
- Friday sets and removes typed properties.
- Friday appends to today's daily note.
- Friday creates a note from an existing template.

### Search and graph

- Friday searches by exact text and returns paths with excerpts.
- Friday returns backlinks and outgoing links for an exact note.
- An indexed vault note can be found by an approximate semantic description.
- Native and semantic results deduplicate to one note candidate.
- Friday projection notes are not re-ingested as independent evidence.

### Continuation

- "Open the second one" uses the active candidate set.
- "Add that there" uses the active note and current Work Item outcome.
- A renamed note remains the same bound note.
- An expired candidate set is not silently reused.

### Editing and conflicts

- A managed-region update preserves user text outside the region.
- A stale full replacement returns `conflict`.
- A rename collision does not overwrite the destination.
- An uncertain mutating operation is reconciled before retry.
- Exactly one durable write is produced for one accepted operation.

### Plugin

- The plugin reconnects after Friday or Obsidian restarts.
- Duplicate operation IDs do not duplicate writes.
- Rename and delete events update Friday bindings.
- The current note and selection can be sent to Friday.
- Friday can open a result in the requested pane through the plugin.

### Bases, tasks, and commands

- Friday queries a Base and receives structured rows.
- Friday creates a `.base` file from a typed specification.
- Friday lists and updates a task using a durable target.
- Friday lists command IDs and executes an exact selected command.
- A command without an observable postcondition is reported as `uncertain`, not falsely successful.

## Suggested executable regression tests

```text
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
test_cli_and_plugin_transports_return_the_same_outcome_shape.py
test_obsidian_tools_are_registered_by_the_organ_not_agent_runtime.py
```

## Key architectural invariants

1. Friday owns the natural-language task and Work Item.
2. Obsidian owns native note editing and workspace behavior.
3. The transport is replaceable; the Friday capability contract is stable.
4. SQLite remains the source of truth for Friday objects.
5. Obsidian remains the source of truth for user-owned Obsidian notes.
6. A managed projection is rebuildable and never treated as independent evidence.
7. A linked note may contain user and Friday-owned regions without either silently overwriting the other.
8. Path is navigation, not durable identity after binding.
9. Tool success is not task completion.
10. Raw CLI or plugin prose never becomes model-visible evidence without normalization.
11. One accepted operation produces at most one durable mutation.
12. A model may select and parameterize capabilities, but it does not write vault files directly.

## Final recommendation

The integration should be built in this order:

```text
Obsidian CLI adapter
    -> Friday-owned tools
    -> vault registry and note identity
    -> operation ledger
    -> first conversational Playbooks
    -> companion plugin and event stream
    -> semantic indexing
    -> Bases and advanced command bridge
```

This order produces value early without locking Friday to one transport.

The existing MemoryVault should remain a simple, rebuildable Friday projection. The new Obsidian Organ should provide the interactive layer around it and around ordinary user vaults.

The intended experience is:

```text
The user talks to Friday.
Friday understands the current work.
Obsidian performs the native note and workspace operation.
Friday remembers the result and can continue from it on the next turn.
```

That is the useful integration boundary. Friday becomes a semantic and conversational control surface for Obsidian, while Obsidian remains the mature human workspace rather than a feature list Friday must rebuild.

## Official Obsidian references

Sources checked on 20 August 2026:

- [Obsidian CLI](https://obsidian.md/help/cli)
- [Obsidian URI](https://help.obsidian.md/Extending%2BObsidian/Obsidian%2BURI)
- [Obsidian Vault developer guide](https://docs.obsidian.md/Plugins/Vault)
- [Build an Obsidian plugin](https://docs.obsidian.md/Plugins/Getting%20started/Build%20a%20plugin)
- [Build a Bases view](https://docs.obsidian.md/plugins/guides/bases-view)
