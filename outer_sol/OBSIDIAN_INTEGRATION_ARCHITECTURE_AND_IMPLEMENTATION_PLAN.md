# Friday Obsidian Integration Architecture and Implementation Plan

> Document ID: FRIDAY-OBS-001  
> Status: External architecture proposal, draft v0.4  
> Repository snapshot: `main`, Friday `0.206.0`, 21 August 2026  
> Primary scenario: a free Android-only user, Telegram as the Friday interface, no Obsidian account requirement, no Obsidian Sync subscription, no desktop Obsidian requirement, one physical phone, and an always-on Friday host.  
> Primary Android sync client: Syncthing-Fork.  
> Related documents: [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), [`MCP_ARCHITECTURE_OBSERVATION.md`](MCP_ARCHITECTURE_OBSERVATION.md), and [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md).

## Revision 0.4

This revision corrects a practical flaw in v0.3: a user who owns only one Android phone cannot scan a QR code displayed on that same phone.

The primary pairing flow is now **copy and paste**, not QR scanning.

```text
Telegram
    -> Copy Friday Device ID
    -> switch to Syncthing-Fork
    -> Add device
    -> paste ID
    -> save
```

QR remains an optional convenience when the Telegram setup page is displayed on another screen. It is never required for the one-phone path.

The important consequences are:

1. The complete supported setup must work on one Android device.
2. Telegram's clipboard button is the preferred handoff.
3. A plain selectable Device ID and HTTPS setup page are mandatory fallbacks.
4. The user pastes only Friday's Syncthing Device ID.
5. Friday discovers the Android Device ID from Syncthing's pending-device state and accepts it automatically within the active setup session.
6. The user never has to copy an Android Device ID back into Telegram.
7. QR-specific wording, acceptance criteria, and implementation tasks are secondary only.

## Hard product assumptions

The primary design assumes:

```text
user side:
    one Android phone or tablet
    Telegram client
    official Obsidian Android application
    Syncthing-Fork
    no desktop computer required

Friday side:
    always-on Linux host or home server
    Friday backend and workers
    writable per-user vault checkout
    managed Syncthing process

not assumed:
    Obsidian account
    paid Obsidian services
    Obsidian Sync
    desktop Obsidian
    desktop CLI
    second screen
    browser-session import
    continuously open Obsidian mobile app
    continuously connected companion plugin
```

Future desktop or account-based transports may attach behind the same contracts, but they must not shape the core free Android workflow.

## Product goal

The user performs one setup procedure and then uses Obsidian through Friday in ordinary Russian from Telegram.

Examples after setup:

```text
Добавь итог разговора в сегодняшнюю заметку.

Найди в Obsidian заметку про проблемы с индексом документов.

Создай заметку в Projects/Friday и добавь туда ссылки на найденные документы.

Поставь в ежедневную заметку задачу проверить аудит завтра.

Добавь заметке статус review и тег friday.

Покажи, какие заметки ссылаются на архитектуру поиска.

Перемести второй результат в Archive/2026.

Собери Base для активных заметок по Friday.

Сохрани результат исследования и дай кнопку, чтобы открыть его в Obsidian.
```

The intended experience is:

```text
The user talks to Friday.
Friday understands and coordinates the task.
Friday reads or changes its server-side vault checkout.
Syncthing-Fork transfers changes between Friday and Android.
Obsidian remains the native mobile editor and viewer.
```

## Functional tiers

### URI-only handoff

A user with only the Obsidian Android app may use `obsidian://` actions to:

```text
open a vault
open a note
create a note
append text to a note
open or create a daily note
open Obsidian search
```

This is a one-tap handoff. Friday cannot reliably read the vault, search all notes, observe edits, confirm URI writes, maintain note identity, or continue a multi-step note workflow.

### Full free Android mode

With Syncthing-Fork connected, Friday may:

```text
list and search notes
read and edit notes while Obsidian is closed
manage properties, tags, tasks, templates, and daily notes
maintain links and backlinks
create .base files
index notes semantically
link Obsidian notes to Friday objects
synchronize changes whenever Android is available
```

### Optional companion mode

A future Friday companion plugin adds foreground-only context:

```text
current note
selected text
cursor insertion
active heading
native Obsidian commands
pane and workspace actions
```

The plugin is never the synchronization backbone.

## Full free Android architecture

```text
┌──────────────────────────────────────────────┐
│ Telegram client on the only Android phone    │
│                                              │
│ /obsidian                                    │
│ ordinary Russian requests                    │
│ Copy Friday ID button                        │
│ Open in Obsidian buttons                     │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Friday                                       │
│                                              │
│ Interaction Control Plane                    │
│ Obsidian Organ                               │
│ note operations and index                    │
│ Work Items and Playbooks                     │
│ operation and delivery ledger                │
└──────────────────────┬───────────────────────┘
                       │ local filesystem
                       ▼
┌──────────────────────────────────────────────┐
│ Per-user server checkout                     │
│                                              │
│ Markdown notes                               │
│ attachments                                  │
│ templates                                    │
│ .base files                                  │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Managed per-user Syncthing profile           │
│                                              │
│ unique server Device ID                      │
│ REST and event adapter                       │
│ folder, peer, and delivery state             │
└──────────────────────┬───────────────────────┘
                       │ Syncthing protocol
                       ▼
┌──────────────────────────────────────────────┐
│ Syncthing-Fork on Android                    │
│                                              │
│ Send & Receive folder                        │
│ Android background synchronization           │
└──────────────────────┬───────────────────────┘
                       │ shared device storage
                       ▼
┌──────────────────────────────────────────────┐
│ Official Obsidian Android app                │
│                                              │
│ opens the same folder as a vault             │
│ native editing, links, views, and plugins    │
└──────────────────────────────────────────────┘
```

## Identity model

There is no Obsidian account binding in the primary path.

```text
Telegram identity
    -> Friday user
        -> Syncthing server profile
        -> Android Syncthing device
        -> logical vault
        -> server checkout
        -> Android vault alias and folder
        -> Obsidian note identities
```

Suggested contracts:

```python
SyncthingProfile(
    id="stprof_...",
    friday_user_id="user_...",
    config_root="...",
    database_root="...",
    api_endpoint="http://127.0.0.1:...",
    server_device_id="...",
    state="running",
)

AndroidSyncDevice(
    id="stdev_...",
    friday_user_id="user_...",
    syncthing_device_id="...",
    display_name="Pixel 10",
    state="connected",
    last_seen_at="...",
)

ObsidianLogicalVault(
    id="obsvault_...",
    friday_user_id="user_...",
    display_name="Friday",
    folder_id="friday-user-...",
    server_path="...",
    android_vault_name="Friday",
    android_path_hint="Documents/Obsidian/Friday",
    sync_device_id="stdev_...",
    state="ready",
)
```

A future sync provider may attach to the logical vault without changing note, search, or Work Item contracts.

## Recommended Syncthing topology

Use one managed Syncthing profile and process per connected Friday user for the first implementation.

Advantages:

- one server Device ID maps to exactly one Friday user;
- the pending Android device cannot be assigned to another user's setup profile;
- device and folder state remain easy to diagnose;
- disconnect and reset affect one user only;
- each profile has its own REST key and event stream;
- one active pairing session has one unambiguous pending-device queue.

Cost:

- one lightweight process and database per connected user;
- additional supervisor entries and local ports;
- more startup and upgrade work.

A pooled daemon may be introduced later behind `VaultSyncTransport`.

Suggested layout:

```text
data/obsidian/
    users/
        <friday-user-id>/
            syncthing-config/
            syncthing-db/
            vaults/
                <logical-vault-id>/
                    .stfolder/
                    Notes/
                    Attachments/
                    Templates/
```

The Syncthing GUI and REST API bind to loopback. Friday uses a generated API key.

## One-phone Telegram onboarding

### Entry point

The user sends:

```text
/obsidian
```

Before pairing, Friday replies:

```text
Obsidian is not connected.

You need two free Android apps:
1. Obsidian
2. Syncthing-Fork

[ Start setup ]
[ Install Obsidian ]
[ Install Syncthing-Fork ]
```

The setup may be displayed as:

- an edited Telegram message with inline buttons;
- a Telegram Mini App;
- a short-lived HTTPS page for clients without adequate Mini App support.

All paths refer to the same resumable onboarding session bound to the Friday user.

### Android storage preflight

The vault must use Android device storage, not Obsidian private app storage, because Syncthing-Fork must access the same folder.

Supported cases:

#### Existing vault

The user already has an Obsidian vault in device storage. During folder acceptance, the user selects that existing folder.

#### New Friday vault

Friday creates an empty server checkout. The user accepts the offered folder into device storage and then opens that folder as a vault in Obsidian.

### Provisioning

Friday durably creates:

```text
one onboarding session
one per-user Syncthing profile
one server Device ID
one logical vault checkout
one unique Syncthing folder ID
```

Provisioning is idempotent. Refreshing or repeating `/obsidian` resumes the same state.

## Primary pairing flow: clipboard first

### Telegram response

Friday presents:

```text
Copy the Friday Device ID, then add it as a remote device in Syncthing-Fork.

[ Copy Friday Device ID ]
[ Open step-by-step guide ]
[ Show QR for another screen ]
```

The first button uses Telegram `InlineKeyboardButton.copy_text`. The copied text is the complete Friday-side Syncthing Device ID.

The Device ID also appears below as selectable monospaced text:

```text
XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX
```

This fallback is required because some unofficial or old Telegram clients may not implement the clipboard button correctly.

The HTTPS setup page also provides:

```text
[ Copy ID ]
```

If browser clipboard access fails, the page selects the full ID for manual copy.

### Android action

The user performs:

```text
1. Tap Copy Friday Device ID in Telegram.
2. Open Syncthing-Fork.
3. Open Devices.
4. Tap Add device.
5. Paste the ID.
6. Use a friendly name such as Friday.
7. Save.
```

No second screen is needed.

### Automatic reverse binding

After Android saves Friday as a device, the Android Syncthing instance attempts to connect. The dedicated Friday Syncthing profile observes an unknown pending device.

Friday then:

1. reads the Android Device ID and reported device name from the pending-device state;
2. checks that exactly one onboarding session is actively waiting on this dedicated profile;
3. records the Android Device ID;
4. adds the Android device to the profile configuration;
5. associates it with the Friday user and logical vault;
6. shares the logical vault folder with it;
7. updates the Telegram message and setup page.

The user does not copy the Android Device ID back to Friday.

If multiple pending devices appear unexpectedly, Friday does not guess. It shows device names and short Device ID suffixes and asks for one selection.

### Optional QR fallback

QR is only an alternate presentation of the same Friday Device ID.

It is useful when:

- Telegram is open on a computer or tablet;
- another screen is available;
- the user explicitly chooses QR.

It is not shown as the default instruction and is not part of the minimum one-phone acceptance path.

The design must not depend on Syncthing-Fork importing a QR image from the same phone's gallery unless that behavior is separately probed and versioned.

## Folder acceptance

After Friday accepts the Android device, Syncthing-Fork receives a folder offer.

The user performs:

```text
1. Accept the Friday Vault folder.
2. Choose or create a device-storage path.
3. Keep folder type Send & Receive.
```

Recommended new-vault path:

```text
Documents/Obsidian/Friday
```

For an existing vault, the user selects the existing device-storage vault folder.

Friday cannot remotely choose an Android filesystem path. This is an unavoidable manual step.

Friday observes folder acceptance and waits for synchronization readiness.

## Registering the folder in Obsidian

For a new vault, the user opens Obsidian and chooses:

```text
Open folder as vault
```

The user selects the synchronized folder and names the vault. Friday recommends the default alias `Friday` so the generated `obsidian://` links work immediately.

For an existing vault, the user supplies or confirms its current Obsidian vault name.

## Round-trip verification

Friday writes:

```text
Friday Connection Test.md
```

Then it:

1. commits the file atomically;
2. requests a local Syncthing scan;
3. waits for the server index to include it;
4. waits for Android remote completion to reach 100 percent while connected;
5. sends an `Open in Obsidian` action;
6. asks the user to confirm that the note opens.

The last confirmation proves Obsidian vault registration and binds the Android vault alias. It is not required to prove the lower-level Syncthing transfer itself.

## Minimum unavoidable actions

Assuming both Android apps are already installed:

```text
1. Send /obsidian.
2. Tap Copy Friday Device ID.
3. Open Syncthing-Fork, add a device, paste, and save.
4. Accept the offered folder and choose its Android path.
5. Open that folder as a vault in Obsidian.
6. Tap the verification or Open in Obsidian button.
```

The one-phone path never requires scanning its own screen.

Reducing the flow further would require either:

- a supported Syncthing-Fork Android intent that pre-fills the Device ID;
- a Friday Android setup helper;
- a Friday Obsidian plugin with its own synchronization transport.

None is a baseline dependency.

## Onboarding state machine

```text
not_connected
    -> provisioning_server_profile
    -> awaiting_device_id_handoff
    -> awaiting_android_device
    -> android_device_detected
    -> offering_folder
    -> awaiting_android_folder_acceptance
    -> initial_sync
    -> awaiting_obsidian_vault_registration
    -> round_trip_verification
    -> ready

recoverable states:
    obsidian_missing
    syncthing_fork_missing
    copy_button_unsupported
    manual_copy_required
    device_storage_required
    device_offline
    multiple_pending_devices
    folder_not_accepted
    folder_path_error
    android_background_restricted
    verification_failed
    reconnect_required

terminal states:
    ready
    cancelled
    disconnected
    failed
```

`awaiting_device_id_handoff` means Friday has exposed the Device ID. It does not require proof that the clipboard button worked. The next observable proof is the pending Android device.

## Telegram status panel

After setup, `/obsidian` displays:

```text
Obsidian: connected
Vault: Friday
Android device: Pixel 10
Server vault: ready
Phone connection: online / offline
Phone sync: current / pending / unknown
Last phone contact: 21:43

[ Open vault ]
[ Test sync ]
[ Reconnect phone ]
[ Show Friday Device ID ]
[ Change vault name ]
[ Disconnect ]
```

During pairing, the same message may be edited as state advances. The user may also reopen `/obsidian` at any time.

## Syncthing management adapter

Implement:

```text
friday/organs/obsidian/
    syncthing_process.py
    syncthing_rest.py
    syncthing_events.py
    syncthing_pairing.py
    syncthing_status.py
```

Required operations:

```text
start or stop one profile
probe version and status
read the complete server Device ID
construct Telegram copy-text payload
render optional QR
list pending devices
add or remove an Android device
create or update a folder
share a folder with a device
request folder scan
read local folder status
read remote-device completion
consume connection and folder events
pause or resume a device
read folder errors
query restart-required state
```

Use Syncthing's local REST and event APIs, not web UI scraping.

Relevant endpoints include:

```text
/rest/system/version
/rest/system/status
/rest/system/connections
/rest/config/devices
/rest/config/folders
/rest/config/restart-required
/rest/cluster/pending/devices
/rest/db/scan
/rest/db/status
/rest/db/completion
/rest/events
```

All responses are normalized into Friday-owned contracts.

## Discovery and connectivity

The server profile must be able to receive the Android connection through supported Syncthing discovery and relay behavior or a configured direct address.

Friday diagnostics should distinguish:

```text
Android has not added Friday yet
Android added Friday but has not connected
peer discovered through global discovery
peer connected directly
peer connected through relay
folder offered but not accepted
folder connected but synchronization incomplete
```

The setup page should not ask the user to configure ports unless automated discovery and relays have failed and diagnostics identify a network problem.

## Version compatibility

Friday should pin and probe supported Syncthing versions.

A startup probe records:

```text
Syncthing version
REST API availability
required endpoint availability
event schema version
server Device ID validity
profile configuration health
```

Syncthing-Fork is outside Friday's release control. The onboarding guide should state a tested minimum version and detect behavior through observable protocol state rather than UI labels alone.

## Sync truth model

Do not collapse synchronization into one Boolean.

```python
VaultDeliveryState(
    local_write_complete=True,
    server_scan_complete=True,
    android_connected=False,
    android_completion=None,
    android_received=False,
    obsidian_opened=False,
)
```

Meaning:

```text
local_write_complete
    Friday committed the note to the server checkout

server_scan_complete
    the server Syncthing index observed the revision

android_connected
    the Android peer is currently connected

android_completion
    the peer completion percentage for the folder

android_received
    Syncthing reports the Android peer has the revision

obsidian_opened
    only a user action or active companion plugin can establish this
```

Friday may say:

```text
The note is saved on the Friday server and will synchronize when the phone is available.
```

or:

```text
The note is saved and Syncthing reports that the Android device received the current folder state.
```

It must not claim that Obsidian displayed the note without evidence.

## Android background constraints

Android may delay or stop Syncthing-Fork in the background.

Onboarding should explain one optional reliability step:

```text
Allow background operation and exclude Syncthing-Fork from aggressive battery optimization if the device vendor requires it.
```

This should be presented after functional setup, not as a wall of permissions before pairing.

Phone offline and background delay are normal states:

```text
server write succeeds
    -> delivery pending
    -> Android reconnects later
    -> transfer completes
```

They are not note-operation failures.

## Conflict and recovery model

Both Friday and Android may edit the same note offline. Syncthing may create a conflict copy.

Friday should:

- detect `sync-conflict` files;
- register a conflict record;
- preserve both versions;
- exclude conflict copies from ordinary canonical note search unless explicitly requested;
- offer compare, keep server, keep Android, or merge;
- record the resolution as a new note revision;
- never delete a conflict copy silently.

Enable a bounded server-side Syncthing versioning policy for replaced and deleted remote files.

## Friday server note service

Because no running Obsidian desktop process is assumed, Friday must own server-side Markdown semantics.

Suggested package:

```text
friday/organs/obsidian/
    __init__.py
    contracts.py
    service.py
    router.py
    worker.py
    tools.py
    playbooks.py
    vault_store.py
    markdown_notes.py
    frontmatter.py
    wikilinks.py
    note_identity.py
    note_merge.py
    task_index.py
    base_spec.py
    indexer.py
    diagnostics.py
```

The service operates only inside the configured server checkout and uses atomic writes, expected revisions, operation IDs, and postcondition checks.

## Friday Organ integration

Obsidian should be a first-party Organ.

The current Organ Protocol contributes capabilities, workers, and routers. Add a code-owned tool registration extension:

```python
class Organ:
    def capabilities(self):
        return ()

    def tools(self, ctx):
        return ()

    def workers(self, ctx):
        return ()

    def router(self):
        return None
```

The Obsidian Organ contributes:

```text
capabilities
    obsidian.connect
    obsidian.read
    obsidian.write
    obsidian.manage

workers
    Syncthing supervisor
    event consumer
    delivery reconciler
    note indexer

router
    onboarding and status API

Friday-owned tools
    note and vault capabilities
```

Do not add a large Obsidian branch inside `AgentRuntime`.

## Note identity and ownership

Use stable frontmatter for notes Friday creates or binds:

```yaml
friday_obsidian_id: obnote_7d18d2f4c9e44a35
```

Optional bindings:

```yaml
friday_object_id: ko_...
friday_raw_object_id: raw_...
friday_projection_kind: linked
friday_projection_revision: 4
```

Identity invariants:

- title is not identity;
- path is not permanent identity after binding;
- content digest is a revision, not identity;
- rename and move preserve the integration ID;
- copied notes receive a new integration ID;
- deleted bindings become tombstones;
- unbound user notes may be read without modifying them.

Ownership modes:

```text
user_owned
    explicit edits only

linked
    Friday may update selected properties and managed regions

friday_managed
    Friday owns the whole note body

projection
    rebuildable mirror, never independently ingested

inbox
    user note selected for Friday ingestion
```

## Managed regions

For linked notes, update marked regions instead of replacing the whole note:

```markdown
<!-- friday:managed:start id="summary" revision="4" -->
## Friday summary

Generated content here.
<!-- friday:managed:end id="summary" -->
```

Rules:

- region IDs are unique within a note;
- user text outside the region is preserved;
- malformed or duplicate markers return `ambiguous`;
- the write uses an expected note revision;
- a successful write returns a new revision digest.

## Capability surface

Expose stable Friday tools, not raw filesystem access.

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

### Daily notes, templates, tasks, and Bases

```text
obsidian_daily_note
obsidian_create_from_template
obsidian_list_templates
obsidian_list_tasks
obsidian_update_task
obsidian_list_bases
obsidian_query_base
obsidian_create_base_item
obsidian_create_or_update_base
```

### Foreground-only plugin tools

```text
obsidian_get_active_note
obsidian_get_selection
obsidian_insert_at_cursor
obsidian_open_in_split
obsidian_list_commands
obsidian_run_command
obsidian_load_workspace
```

Foreground-only tools return `unavailable` when the companion plugin is disconnected. Core Syncthing-backed features remain available.

## Search architecture

Friday combines:

```text
exact path and title lookup
frontmatter properties
tag index
Markdown lexical search
wikilink and backlink graph
semantic passage index
approximate date parsing
active Work Item context
```

Pipeline:

```text
query
    -> exact identity lane
    -> lexical lane
    -> property and tag lane
    -> link graph lane
    -> semantic passage lane
    -> rank fusion
    -> typed candidate set
```

Each candidate returns:

```text
logical vault ID
stable note ID if available
path
title
excerpt
revision
match channels
sync conflict state
last server observation
```

The server checkout is the index source. Syncthing events, filesystem events, reconnect reconciliation, and manual rebuild trigger incremental indexing.

## Projection-loop prevention

The existing `MemoryVault` and any Friday-managed projection roots remain searchable but are marked as `friday_projection`.

```text
Friday Knowledge Object
    -> Markdown projection
    -> Syncthing vault
    -> Friday note index
```

The projected note must not re-enter Friday ingestion as independent evidence.

## Daily notes and templates

Friday stores one stable convention per logical vault:

```python
ObsidianVaultConvention(
    daily_folder="Daily",
    daily_format="YYYY-MM-DD",
    template_folder="Templates",
    attachment_folder="Attachments",
)
```

Defaults are accepted during onboarding without another question and may be changed later.

Template operations should:

- list Markdown templates;
- create a note from a selected template;
- resolve common date and time values;
- fill explicit placeholders;
- preserve unknown syntax;
- ask only for missing required values.

## Properties, links, tasks, and Bases

Property mutations are typed:

```text
text
list
number
checkbox
date
datetime
```

A multi-property update is one atomic frontmatter rewrite. Do not expose arbitrary YAML replacement to the model.

Friday maintains a structured link index for wikilinks, Markdown links, headings, blocks, embeds, resolved targets, and unresolved targets.

Task references prefer block IDs and otherwise use exact text plus nearby context and the observed revision. Line numbers alone are not durable.

Friday may create `.base` files as ordinary vault files. Because no desktop Obsidian engine is assumed, server-side Base queries use a Friday-owned typed `BaseSpec` evaluator over the note index. The generated file should remain compatible with the supported Obsidian Base subset.

## Interaction Control Plane integration

Use Work Items and Active Frames from [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md).

```python
ObsidianActiveFrame(
    logical_vault_id="obsvault_...",
    active_note_id="obnote_...",
    active_path="Projects/Friday.md",
    active_revision="sha256:...",
    active_heading="#Retrieval",
    selected_candidate_set_id="candset_...",
    last_operation_id="obsop_...",
)
```

This supports:

```text
Добавь это туда.
Открой второй результат.
Перемести его в архив.
Теперь свяжи с найденным документом.
Сделай из этого задачи.
```

The model resolves language. Friday validates the durable target and operation state.

## Recommended Playbooks

### ConnectFreeAndroidObsidian

```text
create onboarding session
    -> provision per-user Syncthing profile
    -> expose Friday Device ID with clipboard-first handoff
    -> wait for Android pending device
    -> bind and accept Android device
    -> offer logical vault folder
    -> wait for folder acceptance
    -> initial synchronization
    -> register Obsidian vault alias
    -> round-trip verification
    -> ready
```

### CaptureConversationToObsidian

```text
select conversation range
    -> summarize
    -> resolve vault and destination
    -> resolve template or capture format
    -> create or update note
    -> verify local revision
    -> track Android delivery
    -> return Open in Obsidian action
```

### AppendToDailyNote

```text
resolve local date and vault convention
    -> format capture or task
    -> append idempotently
    -> verify local revision
    -> track delivery
```

### SearchAndOpenObsidianNote

```text
parse query and vault scope
    -> lexical, property, graph, and semantic search
    -> rank and deduplicate
    -> select or ask user
    -> generate exact Obsidian URI
```

### UpdateObsidianMetadata

```text
resolve note
    -> read current properties
    -> validate typed mutations
    -> apply one atomic update
    -> track delivery
```

### LinkFridayObjectToObsidian

```text
resolve Friday object
    -> resolve note
    -> create durable binding
    -> update property or managed region
    -> track delivery
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

## Operation and delivery ledger

Store mutating operations before dispatch:

```text
operation_id
work_item_id nullable
logical_vault_id
method
arguments_digest
expected_revision nullable
status
result_revision nullable
server_scan_state
android_delivery_state
created_at
updated_at
```

Operation states:

```text
prepared
committed
scan_pending
scan_complete
delivery_pending
delivered
conflict
failed
uncertain
reconciled
cancelled
```

Retry rules:

- reads may retry after transient failure;
- create checks target existence and operation ID;
- append uses idempotency markers or postcondition reconciliation;
- replace requires expected revision;
- move checks source and destination;
- delete never blindly retries after an uncertain result.

## Suggested storage projection

```text
obsidian_sync_profiles
    id
    friday_user_id
    config_root
    database_root
    api_endpoint
    api_key_ref
    server_device_id
    state
    created_at
    updated_at

obsidian_android_devices
    id
    friday_user_id
    profile_id
    syncthing_device_id
    display_name
    state
    last_seen_at

obsidian_vaults
    id
    friday_user_id
    profile_id
    android_device_id
    display_name
    folder_id
    server_path
    android_vault_name
    android_path_hint
    state
    convention_json
    created_at
    updated_at

obsidian_onboarding_sessions
    id
    friday_user_id
    state
    setup_token_hash
    telegram_chat_id
    copied_id_exposed_at nullable
    pending_device_id nullable
    expires_at
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

obsidian_operations
    id
    work_item_id nullable
    vault_id
    method
    arguments_digest
    expected_revision nullable
    status
    result_json
    delivery_json
    created_at
    updated_at

obsidian_conflicts
    id
    vault_id
    canonical_path
    conflict_path
    detected_at
    status
    resolution_json nullable
```

## API surface

```text
POST   /api/obsidian/onboarding/start
GET    /api/obsidian/onboarding/{id}
POST   /api/obsidian/onboarding/{id}/select-device
POST   /api/obsidian/onboarding/{id}/vault-alias
POST   /api/obsidian/onboarding/{id}/verify
POST   /api/obsidian/onboarding/{id}/cancel

GET    /api/obsidian/status
POST   /api/obsidian/test-sync
POST   /api/obsidian/reconnect
POST   /api/obsidian/disconnect

GET    /api/obsidian/vaults
GET    /api/obsidian/notes/search
GET    /api/obsidian/notes/read
POST   /api/obsidian/operations
GET    /api/obsidian/operations/{id}

POST   /api/obsidian/index/rebuild
GET    /api/obsidian/index/status
```

The conversational runtime calls the service through Execution Kernel handlers, not through its own HTTP API.

## Diagnostics

Expose:

```text
configured users and vaults
Syncthing version
server profile state
server Device ID validity
pending devices
connected Android device
connection type: direct or relay
folder acceptance
local scan status
remote completion
last Android contact
folder errors
conflict count
operation backlog
index coverage
```

Example:

```text
Obsidian free Android integration: ready
Vault: Friday
Server profile: running
Android device: Pixel 10, online through relay
Folder: up to date
Last Android contact: 21:43
Pending operations: 0
Conflicts: 0
```

## MCP position

MCP is not required for Friday-to-Obsidian integration.

```text
Friday-owned tools
    -> ObsidianService
    -> server note store
    -> managed Syncthing adapter
```

An optional MCP facade may later expose the same stable capabilities to external agents. A generic Obsidian MCP server does not replace device pairing, Android synchronization, note identity, Work Items, or delivery-state tracking.

## Implementation phases

### P0: freeze the one-phone product contract

- make Android-only, one-phone, and no-subscription assumptions executable;
- define copy and paste as the primary pairing flow;
- define the plain-text and HTTPS clipboard fallbacks;
- make QR optional only;
- define sync truth, onboarding states, and error taxonomy;
- select tested Syncthing and Syncthing-Fork versions.

Deliverable: versioned contracts and an executable compatibility probe.

### P1: managed Syncthing runtime

- package or require Syncthing on the Friday host;
- create per-user config and database roots;
- supervise one process per connected user;
- implement REST authentication and version probe;
- implement pending-device, folder, scan, completion, and event adapters;
- add diagnostics and restart recovery.

Deliverable: Friday creates and monitors one isolated user profile.

### P2: clipboard-first Telegram onboarding

- add `/obsidian` status panel;
- add Telegram `copy_text` button;
- display the exact Device ID as selectable text;
- add Mini App or HTTPS guide with copy fallback;
- leave QR behind an optional second-screen action;
- detect the Android pending device;
- auto-accept it within the dedicated setup profile;
- offer the folder;
- wait for folder acceptance;
- collect vault alias;
- perform round-trip verification;
- make the flow resumable and idempotent.

Deliverable: one user completes setup on one Android phone with no QR scan.

### P3: native server note operations

Implement:

```text
list
search
read
create
append
prepend
replace
move
delete
properties
tags
daily notes
templates
```

Add atomic writes, expected revisions, and operation ledger.

### P4: delivery and conflict handling

- track local write, server scan, peer connection, and Android receipt separately;
- return pending delivery without blocking;
- detect conflict files;
- add compare and resolution flow;
- enable server-side versioning;
- add reconnect reconciliation.

### P5: note identity, links, and graph

- add stable note IDs and bindings;
- build link index;
- implement backlinks, unresolved links, orphans, and dead ends;
- implement move with bounded link updates;
- preserve identity across rename and move.

### P6: semantic indexing and operational memory

- index configured vault roots;
- combine lexical, property, graph, and semantic lanes;
- add `ObsidianActiveFrame`;
- persist candidate sets;
- implement Playbooks and Completion Gates;
- combine Obsidian notes with Friday documents and conversations.

### P7: tasks, Bases, and advanced note semantics

- durable task targeting;
- typed `BaseSpec` and `.base` generation;
- managed regions;
- Friday-to-Obsidian bindings;
- explicit note ingestion into Friday Inbox.

### P8: optional Android companion plugin

- pair plugin with existing Friday user and vault;
- expose current note and selection;
- insert at cursor;
- run native commands;
- open panes and views;
- keep plugin out of synchronization.

### P9: optional future transports

- pooled Syncthing daemon;
- supported Android intent or helper for prefilled Device ID;
- alternative free sync providers;
- desktop CLI adapter;
- MCP facade;
- packaged companion plugin release.

## Suggested first release

```text
one Friday user
one Android device
one logical vault
per-user Syncthing profile
Telegram /obsidian onboarding
copy-text Device ID handoff
manual selectable-ID fallback
optional QR only
pending-device auto-accept
folder-offer detection
round-trip verification
server note list/search/read/create/append
properties
daily notes
operation ledger
precise delivery status
Obsidian URI open button
basic diagnostics
```

Do not block the release on:

```text
companion plugin
community-plugin commands
custom Bases view
Canvas
multiple phones
multiple vaults per user
pooled Syncthing runtime
```

## Acceptance criteria

### One-phone onboarding

- A user completes setup with one Android phone and no second screen.
- No Obsidian account, email, password, or subscription is required.
- `/obsidian` starts setup from the correct Friday identity.
- The primary Telegram button copies the complete Friday Device ID.
- The Device ID fits the Telegram copy-text contract.
- The message also contains a selectable full Device ID.
- An unsupported copy button falls back to manual copy or the HTTPS page.
- QR is not required by any happy-path state or test.
- Friday observes the Android pending device after the user pastes and saves the ID.
- The user never copies the Android Device ID back into Friday.
- One active dedicated profile binds the pending device to the correct user.
- Multiple unexpected pending devices require explicit selection.
- Friday offers exactly one logical vault folder.
- Setup waits for folder acceptance.
- The round-trip note reaches Android remote completion while connected.
- Refreshing setup does not duplicate profiles, devices, folders, or notes.

### Android operation

- Friday writes while Android is offline.
- Offline delivery is pending, not failed.
- Reconnection transfers pending revisions.
- Friday distinguishes server write from Android receipt.
- Friday never claims Obsidian opened a note without evidence.
- Open links use the configured Android vault alias.

### Notes and conflicts

- Friday creates, reads, appends, prepends, replaces, moves, and deletes notes.
- Full replacement uses expected revision.
- Typed property updates preserve the body.
- Daily-note paths follow the stored convention.
- Managed-region updates preserve user text.
- Syncthing restart preserves pairing and folder state.
- Conflict copies are preserved and surfaced.
- Uncertain mutations are reconciled before retry.
- One accepted operation produces at most one durable mutation.

### Search and composition

- Exact path and title search work.
- Lexical search returns path and excerpt.
- Semantic search finds approximate content.
- Backlinks and outgoing links are computed without desktop Obsidian.
- A move reports updated and unresolved links.
- Friday projections are not re-ingested as independent evidence.
- Follow-ups use the active candidate set and note frame.

### Companion independence

- All core operations work with no plugin installed.
- Plugin offline state does not mark the vault unhealthy.
- Installing the plugin adds active-note features without changing the Syncthing binding.

## Suggested regression tests

```text
test_free_android_setup_requires_no_obsidian_account.py
test_one_phone_setup_never_requires_qr.py
test_copy_text_button_contains_the_exact_server_device_id.py
test_server_device_id_fits_the_telegram_copy_limit.py
test_selectable_device_id_is_always_present.py
test_unsupported_copy_button_has_an_https_and_manual_fallback.py
test_qr_is_optional_and_never_a_happy_path_dependency.py
test_android_pending_device_is_discovered_after_manual_paste.py
test_user_never_has_to_return_the_android_device_id.py
test_each_user_profile_has_a_unique_server_device_id.py
test_the_pending_device_binds_to_the_correct_profile.py
test_multiple_pending_devices_require_selection.py
test_folder_acceptance_is_required_before_ready.py
test_round_trip_verification_reaches_remote_completion.py
test_refreshing_setup_does_not_duplicate_resources.py

test_friday_can_write_while_android_is_offline.py
test_offline_delivery_is_pending_not_failed.py
test_android_receipt_is_distinct_from_server_write.py
test_friday_never_claims_mobile_obsidian_opened_without_evidence.py
test_the_open_uri_uses_the_configured_vault_alias.py

test_friday_can_create_and_append_to_an_obsidian_note.py
test_friday_can_append_to_the_daily_note_once.py
test_a_stale_full_replacement_returns_conflict.py
test_a_note_rename_preserves_the_integration_identity.py
test_a_move_updates_resolvable_links_and_reports_ambiguous_links.py
test_a_managed_region_preserves_user_text.py
test_typed_properties_preserve_markdown_body.py

test_syncthing_restart_preserves_device_and_folder_configuration.py
test_an_uncertain_append_is_reconciled_before_retry.py
test_a_sync_conflict_becomes_a_user_visible_conflict_record.py
test_conflict_files_are_not_deleted_without_resolution.py
test_remote_completion_updates_delivery_state.py

test_native_and_semantic_results_deduplicate.py
test_android_changes_reindex_only_the_changed_note.py
test_delete_events_remove_note_passages.py
test_a_friday_projection_is_not_reingested_as_new_knowledge.py
test_the_second_result_uses_the_active_candidate_set.py
test_add_that_there_uses_the_active_note.py

test_core_operations_need_no_companion_plugin.py
test_companion_plugin_offline_does_not_break_syncthing_operations.py
test_obsidian_tools_are_registered_by_the_organ_not_agent_runtime.py
```

## Architectural invariants

1. The primary path works with one Android phone.
2. The primary path requires no Obsidian account or subscription.
3. Copy and paste is the default Device ID handoff.
4. QR is optional and assumes another display.
5. The user copies only Friday's Device ID.
6. Friday learns the Android Device ID from pending-device state.
7. Friday binds to an Android Syncthing device and logical vault, not a cloud account.
8. Syncthing-Fork is the persistent transport.
9. Obsidian URI is navigation, not synchronization proof.
10. The companion plugin is optional foreground context.
11. Friday owns the natural-language task and Work Item.
12. Obsidian owns native mobile viewing and editing.
13. The server checkout is Friday's operational vault copy.
14. Local write, server scan, Android receipt, and Obsidian open are separate postconditions.
15. Path is navigation, not durable note identity after binding.
16. Projections are rebuildable and never independent evidence.
17. One accepted operation produces at most one durable mutation.
18. Offline Android delivery never erases a committed server result.
19. Conflicts preserve both versions until explicit resolution.
20. Core features do not depend on a running Obsidian process.
21. The sync provider remains replaceable behind Friday-owned contracts.

## Final recommendation

Build in this order:

```text
per-user Syncthing runtime
    -> clipboard-first Telegram onboarding
    -> pending Android device auto-binding
    -> Android folder acceptance
    -> round-trip verification
    -> native server note operations
    -> precise delivery status
    -> conflict handling
    -> note identity and link graph
    -> semantic indexing and Work Items
    -> optional companion plugin
```

The existing `MemoryVault` remains a rebuildable Friday projection. The Obsidian Organ operates ordinary user vaults through a managed server checkout and Syncthing-Fork.

The final free one-phone Android experience is:

```text
The user installs Obsidian and Syncthing-Fork once.
The user sends /obsidian.
The user taps Copy Friday Device ID.
The user pastes the ID into Add device in Syncthing-Fork.
Friday discovers and accepts the Android device automatically.
The user accepts one folder and opens it as an Obsidian vault.
After that, the user talks to Friday in Telegram.
Friday searches, creates, edits, links, and organizes notes.
Syncthing-Fork transfers changes whenever Android is available.
Friday reports whether delivery is complete or pending.
A one-tap URI opens the result in the official Obsidian app.
```

This is achievable without a subscription, without a desktop, without a second screen, and without building a complete proprietary synchronization engine inside Friday.

## Official and project references

Sources checked on 21 August 2026:

- [Telegram Bot API: InlineKeyboardButton and CopyTextButton](https://core.telegram.org/bots/api)
- [Obsidian for Android](https://obsidian.md/help/android)
- [Obsidian: Sync your notes across devices](https://obsidian.md/help/sync-notes)
- [Obsidian URI](https://help.obsidian.md/Extending%2BObsidian/Obsidian%2BURI)
- [Obsidian mobile plugin development](https://docs.obsidian.md/Plugins/Getting%20started/Mobile%20development)
- [Syncthing documentation](https://docs.syncthing.net/)
- [Syncthing getting started](https://docs.syncthing.net/intro/getting-started.html)
- [Syncthing REST API](https://docs.syncthing.net/dev/rest.html)
- [Syncthing configuration API](https://docs.syncthing.net/rest/config.html)
- [Syncthing event API](https://docs.syncthing.net/dev/events.html)
- [Syncthing remote completion](https://docs.syncthing.net/rest/db-completion-get.html)
- [Syncthing file versioning](https://docs.syncthing.net/users/versioning.html)
- [Syncthing-Fork on F-Droid](https://f-droid.org/packages/com.github.catfriend1.syncthingfork/)
- [Syncthing-Fork source](https://github.com/Catfriend1/syncthing-android)
