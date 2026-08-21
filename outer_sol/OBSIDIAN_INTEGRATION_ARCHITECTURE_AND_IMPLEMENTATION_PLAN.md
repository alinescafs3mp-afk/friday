# Friday Obsidian Integration Architecture and Implementation Plan

> Document ID: FRIDAY-OBS-001  
> Status: External architecture proposal, draft v0.3  
> Repository snapshot: `main`, Friday `0.206.0`, 21 August 2026  
> Primary scenario: free Android-only user, Telegram as the Friday interface, no Obsidian Sync subscription, no desktop Obsidian requirement, and an always-on Friday host.  
> Primary Android sync client: Syncthing-Fork.  
> Related documents: [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), [`MCP_ARCHITECTURE_OBSERVATION.md`](MCP_ARCHITECTURE_OBSERVATION.md), and [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md).

## Revision 0.3

This revision replaces the account-centric and Obsidian-Sync-centric design from v0.2.

The system must now work under the following hard assumption:

> Obsidian Sync does not exist as an available product. The user has an Android phone, a free local Obsidian vault, Telegram, and no desktop computer that Friday may rely on.

The primary architecture is therefore:

```text
Telegram on Android
    -> Friday
    -> server-side vault checkout
    -> managed Syncthing instance
    -> Syncthing protocol
    -> Syncthing-Fork on Android
    -> Android device-storage folder
    -> official Obsidian mobile app
```

The consequences are important:

1. No Obsidian account login is required.
2. Friday does not bind to an Obsidian account.
3. Friday binds one Friday user to one Android Syncthing device and one logical vault.
4. Syncthing-Fork is the persistent delivery channel.
5. Obsidian URI is the one-tap navigation channel.
6. A Friday companion plugin is optional foreground context, not the synchronization backbone.
7. Friday must implement server-side note semantics that were previously delegated to Obsidian desktop or CLI.
8. Android background execution and synchronization delay are normal operating conditions, not exceptional failures.

## Non-negotiable deployment assumptions

The main design assumes:

```text
user device:
    Android phone or tablet
    official Obsidian mobile application
    Syncthing-Fork
    Telegram client

Friday side:
    always-on Linux host or home server
    writable per-user vault checkout
    managed Syncthing process
    Friday backend and workers

not assumed:
    paid Obsidian services
    Obsidian account
    desktop Obsidian
    desktop CLI
    browser-session import
    continuously running Obsidian mobile app
    continuously connected mobile companion plugin
```

Desktop and paid-provider transports may be added later, but they must not shape the core contracts or acceptance criteria of this plan.

## Product goal

A user should perform one setup procedure and then use Obsidian through Friday in ordinary Russian from Telegram.

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
Friday changes or searches its server-side vault checkout.
Syncthing-Fork carries the change to or from Android.
Obsidian remains the native mobile editor and viewer.
```

## What is available before full synchronization

A user with only the Obsidian Android app may use an URI-only degraded mode.

Friday may return `obsidian://` actions that ask the phone to:

```text
open a vault
open a note
create a note
append text to a note
open or create a daily note
open Obsidian search
```

This mode is useful for immediate capture with one user tap.

It does not give Friday durable access to the vault. Friday cannot reliably:

```text
read existing notes
search the complete vault
confirm that a URI write succeeded
edit while the user is offline
observe phone-side edits
maintain backlinks or note identity
continue a multi-step vault workflow
```

URI-only mode must be presented as a handoff, not as full integration.

## Full free Android architecture

```text
┌──────────────────────────────────────────────┐
│ Telegram client on Android                   │
│                                              │
│ /obsidian                                    │
│ ordinary Russian requests                    │
│ Open in Obsidian buttons                     │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Friday                                       │
│                                              │
│ Interaction Control Plane                    │
│ Obsidian Organ                               │
│ vault operations                             │
│ note index and backlinks                     │
│ Work Items and Playbooks                     │
│ operation ledger                             │
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
│ Managed Syncthing instance                   │
│                                              │
│ unique server Device ID                      │
│ REST and event adapter                       │
│ folder and peer status                       │
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
│ native editing, links, views, plugins         │
└──────────────────────────────────────────────┘
```

## There is no Obsidian account binding

In this architecture, an Obsidian account is irrelevant.

The durable identity graph is:

```text
Telegram identity
    -> Friday user
        -> Syncthing server profile
        -> Android Syncthing Device ID
        -> logical vault
        -> server checkout
        -> Android vault alias and local folder
```

Suggested records:

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
    android_path_hint="Documents/Friday",
    sync_device_id="stdev_...",
    state="ready",
)
```

Do not create an `ObsidianAccountConnection` in the primary free path.

A future paid or delegated account provider may attach to the logical vault without changing note, search, or Work Item contracts.

## Recommended server topology

### Default: one Syncthing profile per Friday user

The simplest functionally correct topology is one managed Syncthing profile and process per connected Friday user.

Advantages:

- one unique server Device ID maps unambiguously to one Friday user;
- QR pairing cannot attach a phone to the wrong user profile;
- device and folder state are easy to reason about;
- disconnect and reset are local to one user;
- per-user event streams and REST credentials are simple;
- no cross-user folder configuration exists inside one Syncthing process.

Cost:

- one additional lightweight process and database per connected user;
- more local ports and supervisor entries;
- more startup and upgrade work.

For Friday's current scale, this trade is preferable to a pooled daemon with a complicated pairing broker.

A pooled multi-user Syncthing daemon may be introduced later behind the same `VaultSyncTransport` contract.

### Suggested directory layout

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

The Syncthing GUI and REST API bind only to loopback. Friday talks to it through a generated API key.

## One-time Telegram onboarding

## Entry point

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

The setup button opens either:

- a Telegram Mini App in clients that support it;
- a short-lived normal HTTPS link in clients that do not.

The setup page is bound to the existing Friday user. It does not ask for an Obsidian login.

## Android preflight

The page explains one important Android choice:

> The Obsidian vault must use device storage, not private app storage, because Syncthing-Fork must access the same folder.

Supported cases:

### Existing Android vault

The user already has an Obsidian vault in device storage.

During folder acceptance, the user selects that existing folder.

### New Friday vault

Friday creates an empty server checkout and offers it as a Syncthing folder.

The user accepts the folder into a device-storage location and then opens that folder as a vault in Obsidian.

## Provisioning

Friday creates:

```text
one onboarding session
one per-user Syncthing profile
one server Device ID
one empty logical vault checkout
one unique Syncthing folder ID
```

The onboarding state is persisted before the process is started.

## Device pairing

The setup page displays:

```text
1. Open Syncthing-Fork.
2. Tap Add device.
3. Scan this QR code.
4. Save the device.
```

The QR contains the Friday-side Syncthing Device ID. Device IDs are public identifiers used for mutual device configuration, not account passwords.

Because the server Device ID belongs to this user's dedicated Syncthing profile, the next pending Android device on that profile is unambiguous.

Friday polls the Syncthing pending-device endpoint or consumes the corresponding event.

When the Android device attempts to connect, Friday:

1. records its Device ID and reported name;
2. adds it to the per-user Syncthing configuration;
3. associates it with the onboarding session;
4. shares the logical vault folder with it.

The page changes to:

```text
Android device found: Pixel 10

Now accept the offered folder in Syncthing-Fork.
```

An explicit confirmation may be shown if the device name or pairing timing is ambiguous.

## Folder acceptance

Syncthing-Fork receives a folder offer.

The user performs the unavoidable Android-side action:

```text
1. Accept the folder.
2. Choose or create the device-storage folder used by Obsidian.
3. Keep folder type Send & Receive.
```

Friday cannot choose an Android filesystem folder remotely. This is the irreducible manual step in a third-party free sync design.

After the Android client accepts the folder, Friday observes `remoteState=valid` and waits for folder completion.

## Registering the folder in Obsidian

For a new vault, the user opens Obsidian once and chooses:

```text
Open folder as vault
```

Then the user selects the same device-storage folder and assigns the desired vault name.

For an existing vault, this step is already complete.

The setup page asks for the local Obsidian vault name only so Friday can generate correct `obsidian://` links later.

## Round-trip verification

Friday writes a small note:

```text
Friday Connection Test.md
```

Then it:

1. requests a local Syncthing scan;
2. waits for the server index to contain the note;
3. waits for the Android device completion status to reach 100 percent while connected;
4. shows an `Open in Obsidian` button;
5. asks the user to confirm that the note opens.

The final confirmation binds the Android vault alias to the logical vault.

Friday may remove the test note after confirmation or keep it as a short onboarding guide.

## Minimum unavoidable user actions

For a new Android-only user, the realistic one-time sequence is:

```text
1. Install Obsidian.
2. Install Syncthing-Fork.
3. Send /obsidian and open setup.
4. Scan one QR code in Syncthing-Fork.
5. Accept one folder and choose its Android path.
6. Open that folder as a vault in Obsidian.
7. Tap the verification link once.
```

This is more work than an OAuth login because no central sync account exists.

Reducing the flow below this level would require Friday to ship its own Android application or its own Obsidian sync plugin with a custom transport.

## Onboarding state machine

```text
not_connected
    -> provisioning_server_profile
    -> awaiting_android_device
    -> android_device_detected
    -> offering_folder
    -> awaiting_android_folder_acceptance
    -> initial_sync
    -> awaiting_obsidian_vault_registration
    -> round_trip_verification
    -> ready

recoverable states:
    syncthing_fork_missing
    device_storage_required
    device_offline
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

Refreshing the setup page must resume the same state instead of creating another profile or folder.

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
[ Change vault name ]
[ Reconnect phone ]
[ Disconnect ]
```

There is no account email because no Obsidian account is involved.

## Syncthing management adapter

Implement a dedicated service:

```text
friday/organs/obsidian/
    syncthing_process.py
    syncthing_rest.py
    syncthing_events.py
    syncthing_pairing.py
    syncthing_status.py
```

The adapter should use the local Syncthing REST and event APIs, not scrape its web UI.

Required operations:

```text
start or stop one profile
probe version and status
read server Device ID
render Device ID QR
list pending devices
add or remove an Android device
create or update a folder
share a folder with a device
query restart-required state
request folder scan
read local folder status
read remote-device completion
consume connection and folder events
pause or resume a device
read folder errors
```

Useful Syncthing endpoints include:

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

All raw responses are normalized into Friday-owned contracts before reaching planners or models.

## Version compatibility

Syncthing's REST surface can evolve. Friday should maintain a probed compatibility profile:

```python
SyncthingRuntimeProfile(
    version="v2.1.0",
    supported=True,
    pending_devices=True,
    granular_config=True,
    remote_completion=True,
    folder_completion_events=True,
)
```

Startup should fail the Obsidian Organ closed when the active Syncthing version is outside the tested range.

The Android app distribution URL should be configuration-owned. Do not hardcode one maintainer repository forever. The default recommendation may point to the F-Droid package for Syncthing-Fork, while the source repository and update channel remain replaceable metadata.

## Recommended Syncthing folder configuration

Primary folder type:

```text
Send & Receive
```

Recommended server-side settings:

```text
filesystem watcher enabled
bounded rescan interval
ignore permissions where cross-platform metadata causes churn
auto normalization enabled
conflict copies allowed
versioning enabled on the server side
no nested Syncthing shares
```

### Versioning

Syncthing is synchronization, not backup.

Enable server-side file versioning so that remote Android replacements and deletions retain recoverable previous versions for a configured period.

This protects against accidental edits arriving from Android. It does not replace Friday's ordinary backup system, and it does not archive server-local changes before they are made.

### `.obsidian` settings folder

The default first release should synchronize the complete vault, including `.obsidian`, because it minimizes user setup and keeps mobile configuration portable.

A later per-vault option may provide:

```text
shared settings
    -> sync .obsidian

device-local settings
    -> ignore .obsidian or selected workspace files
```

Friday itself should never require `.obsidian` settings to perform basic note operations. It should store daily-note, template, and attachment conventions in its own vault configuration after learning or asking for them.

### Ignore patterns

A conservative optional ignore profile may exclude temporary or backup artifacts, but must not silently exclude user notes.

Ignore patterns are per Syncthing folder and are not themselves synchronized automatically. The onboarding UI should not require manual `.stignore` editing in the first release.

## Sync truth model

One word, `synced`, is not precise enough.

Friday should track separate postconditions:

```text
write_committed
    Friday atomically wrote the server file

server_scan_complete
    Syncthing indexed the server-side change

android_peer_connected
    the Android device currently has a Syncthing connection

android_folder_accepted
    the Android device shares the folder back

android_completion_known
    Friday has a current remote completion report

android_received
    remote completion is 100 percent for the relevant folder while state is valid

obsidian_opened
    the user invoked an Obsidian URI or the companion plugin confirmed navigation
```

Friday must not collapse these into one success claim.

Example response when the phone is offline:

```text
The note was updated on Friday's server. Your phone is currently offline, so delivery is pending.
```

Example response after remote completion:

```text
The note was updated and Syncthing reports the Android vault as current.
```

Neither response claims that the user has opened or read the note.

## Outbound write sequence

```text
user request
    -> resolve exact vault and note
    -> obtain or verify expected revision
    -> atomically mutate server file
    -> persist operation result
    -> request Syncthing scan when needed
    -> observe server index
    -> observe remote completion if peer is connected
    -> return precise delivery state
    -> include Open in Obsidian action when useful
```

A user-visible response should not wait indefinitely for an offline phone. Delivery observation has a bounded deadline and may continue as a background operation state.

## Inbound Android edit sequence

```text
user edits note in Obsidian Android
    -> Syncthing-Fork detects local file change
    -> change is transferred to Friday host
    -> server Syncthing applies file
    -> Syncthing event or filesystem watcher identifies change
    -> Friday updates note revision and index
    -> active Work Items invalidate stale reads if necessary
```

Friday must distinguish its own operation from an Android-originated edit to avoid unnecessary loops.

## Android background behavior

Syncthing-Fork may be delayed or stopped by Android background and battery policies.

The onboarding and diagnostics UI should help the user:

```text
allow background operation
remove restrictive battery optimization when required
allow network access under the desired conditions
select whether mobile data is allowed
verify last successful contact
run a manual synchronization test
```

These steps should be advisory and device-specific. Friday should not pretend it can configure Android power management remotely.

Expected behavior while the app is stopped:

```text
Friday continues changing the server checkout.
Operations remain durable.
The Android delivery state becomes pending or unknown.
Synchronization resumes when Syncthing-Fork runs and reconnects.
```

## Conflict handling

Syncthing detects concurrent file changes and may create `sync-conflict` copies.

Friday must treat conflict files as first-class operational events.

```text
conflict detected
    -> preserve both files
    -> bind conflict to the logical note
    -> stop automatic full-note replacement for that note
    -> show a user-visible conflict state
    -> offer compare, keep server, keep Android, or merge
```

For Markdown text, Friday may generate a proposed merge, but the original files remain available until the user chooses a resolution.

Do not automatically delete conflict copies.

Do not index both copies as unrelated independent notes without a conflict relationship.

Suggested model:

```python
ObsidianSyncConflict(
    id="obsconf_...",
    vault_id="obsvault_...",
    canonical_path="Projects/Friday.md",
    conflict_path="Projects/Friday.sync-conflict-....md",
    detected_at="...",
    state="awaiting_resolution",
)
```

## Friday note-operation service

Because no desktop Obsidian process is assumed, Friday needs a native server-side note service.

```text
friday/organs/obsidian/
    note_store.py
    frontmatter.py
    markdown_links.py
    tasks.py
    templates.py
    daily_notes.py
    bases.py
    note_identity.py
    note_merge.py
    indexer.py
```

The model never writes filesystem paths directly. It calls Friday-owned capabilities through Execution Kernel.

## Note identity

Obsidian naturally navigates by vault-relative path, but path changes after rename or move.

Friday-managed or bound notes should contain:

```yaml
friday_obsidian_id: obnote_7d18d2f4c9e44a35
```

Optional links:

```yaml
friday_object_id: ko_...
friday_raw_object_id: raw_...
friday_projection_kind: linked
friday_projection_revision: 4
```

For an unbound user note, temporary identity is:

```text
logical vault ID + exact path + observed revision
```

A durable ID is inserted only when Friday creates, links, or manages the note, or when the user explicitly enables ID assignment for indexed notes.

Identity rules:

```text
title is not identity
path is not permanent identity after binding
content digest is revision, not identity
copy creates a new identity
rename preserves identity
move preserves identity
delete tombstones identity
```

## Revision and write model

Every read returns:

```text
logical vault ID
path
integration ID when present
content revision digest
modified time
source device when known
```

Every full replacement accepts an expected revision.

Possible outcomes:

```text
success
unchanged
not_found
ambiguous
conflict
invalid_note
sync_pending
sync_unknown
unsupported
unavailable
uncertain
failed
```

Mutations use atomic temporary-file replacement inside the server checkout.

## Editing ownership modes

### User-owned

Friday reads and explicitly edits the note when requested. It does not refresh content automatically.

### Linked

Friday may modify selected properties and explicitly marked managed regions while preserving user text.

### Friday-managed

Friday owns the complete body and may regenerate it from Friday state or a Work Item outcome.

### Projection

The note is a rebuildable mirror such as the current `MemoryVault`. Edits are not imported as authoritative changes.

### Inbox note

The note is user-authored and selected for explicit ingestion into Friday's ordinary review pipeline.

## Managed regions

Linked notes use bounded markers:

```markdown
<!-- friday:managed:start id="summary" revision="4" -->
## Friday summary

Generated content here.
<!-- friday:managed:end id="summary" -->
```

Rules:

- Friday updates only the selected region;
- user text outside the markers is preserved;
- malformed or duplicate markers return `ambiguous`;
- one operation produces one new note revision;
- managed regions are not nested;
- a conflict blocks automatic region refresh until reconciled.

## Move and rename semantics

A filesystem rename performed on the server does not automatically guarantee that Obsidian has updated all links.

Therefore `obsidian_move_note` must expose:

```text
update_links = true | false
```

When `update_links=true`, Friday uses its own vault link index to update:

```text
wikilinks
Markdown links that target the moved note
known embeds
bindings and candidate references
```

The operation is planned as one multi-file transaction with an operation ledger and expected revisions.

If Friday cannot safely rewrite an ambiguous link, it returns a partial outcome and lists unresolved references.

A companion plugin may later delegate rename to native Obsidian while the app is open, but the server-side implementation remains required for background Android operation.

## Friday-owned capability surface

### Connection and status

```text
obsidian_connection_status
obsidian_test_sync
obsidian_disconnect
obsidian_reconnect_device
```

### Discovery and navigation

```text
obsidian_list_vaults
obsidian_list_notes
obsidian_search_notes
obsidian_recent_notes
obsidian_open_note
obsidian_open_search
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

### Bases

```text
obsidian_list_bases
obsidian_query_base
obsidian_create_base_item
obsidian_create_or_update_base
```

### Foreground-only companion capabilities

```text
obsidian_get_active_note
obsidian_get_selection
obsidian_insert_at_cursor
obsidian_open_in_split
obsidian_list_commands
obsidian_run_command
obsidian_load_workspace
```

Foreground-only capabilities are unavailable when the mobile companion plugin is disconnected. Their absence must not break the normal Syncthing-backed feature set.

## Capability matrix

| Capability | URI-only | Syncthing-Fork full mode | Companion plugin active |
|---|---:|---:|---:|
| Open note | user tap | user tap | yes |
| Create or append by URI | user tap | yes | yes |
| Read existing note | no | yes | yes |
| Search all notes | no | yes | yes |
| Semantic search | no | yes | yes |
| Edit while Obsidian is closed | no | yes | not required |
| Properties and tags | no | yes | yes |
| Daily notes | limited handoff | yes | yes |
| Templates | limited | yes | yes |
| Tasks | no | yes | yes |
| Backlinks and outgoing links | no | yes | yes |
| Create `.base` files | no | yes | yes |
| Query Friday's BaseSpec | no | yes | yes |
| Current note and selection | no | no | yes |
| Insert at cursor | no | no | yes |
| Run community-plugin command | no | no | yes |
| Work while phone is offline | no | server write pending | no foreground action |

## Search architecture

Friday should combine:

```text
exact path and title lookup
frontmatter property lookup
tag index
Markdown lexical search
backlink and outgoing-link index
Friday semantic passage index
approximate date parsing
active Work Item context
```

Unified search:

```text
query
    -> exact identity lane
    -> lexical note lane
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

## Indexing Android-originated changes

The server checkout is the index source.

Triggers:

```text
Syncthing RemoteChangeDetected events
filesystem watcher events
folder scan completion
reconnect reconciliation
manual rebuild
```

The indexer should debounce repeated updates and index only the affected note.

On rename, preserve note identity and update the path.

On delete, invalidate passages and candidate references.

On conflict, index the canonical file and attach the conflict copy as a related operational artifact.

## Avoiding projection loops

The existing `MemoryVault` and any Friday-managed projection roots must be marked as projections and excluded from automatic ingestion as independent evidence.

```text
Friday Knowledge Object
    -> Markdown projection
    -> Syncthing vault
    -> Friday note index
```

This note may be searchable and openable, but it must retain source kind `friday_projection` and must not re-enter the ingestion pipeline as new knowledge.

## Daily-note conventions

Without desktop Obsidian configuration, Friday needs its own per-vault convention:

```python
ObsidianVaultConvention(
    daily_folder="Daily",
    daily_format="YYYY-MM-DD",
    template_folder="Templates",
    attachment_folder="Attachments",
)
```

During onboarding, defaults are accepted without another question. The user may change them later through `/obsidian` or ordinary language.

Friday may inspect known `.obsidian` core-plugin configuration files when present, but those files are advisory and version-sensitive. Friday's stored convention is the stable contract.

## Templates

Friday should:

- list Markdown templates from the configured folder;
- create a note from a selected template;
- resolve common date and time variables;
- fill explicitly named placeholders;
- preserve unknown syntax;
- ask only for missing required fields;
- synchronize the final note through the normal write path.

## Properties and tags

Use typed mutations:

```text
text
list
number
checkbox
date
datetime
```

A multi-property change is one atomic frontmatter rewrite.

Do not expose arbitrary YAML string replacement to the model.

## Links and backlinks

Friday maintains a structured link index:

```text
source note
raw link text
display text
target path or unresolved target
heading or block subpath
embed flag
resolved state
```

This supports:

```text
backlinks
outgoing links
unresolved links
orphan notes
dead ends
safe rename planning
related-note navigation
```

## Tasks

A task reference should include:

```text
vault ID
note ID or path
block ID when available
line and nearby text fallback
observed revision
status
text excerpt
```

Line numbers alone are not durable.

Task changes use expected revision and reconcile by block ID or exact text plus local context.

## Bases

Friday can create and edit `.base` files as ordinary vault files.

Because no running desktop Obsidian engine is assumed, querying a Base on the server should use a Friday-owned typed `BaseSpec` evaluator against the note index.

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

Friday writes the corresponding `.base` representation for Obsidian mobile and uses the typed specification for its own results.

A future companion plugin may query the native Bases engine when Obsidian is open.

## Optional Android companion plugin

The companion plugin is useful but not required for synchronization.

It adds:

```text
current note
current heading
selected text
insert at cursor
open in split
native command execution
community-plugin commands
workspace context
```

Pairing:

```text
Friday displays a short pairing code
    -> user enters it in the plugin
    -> plugin binds to the existing Friday user and logical vault
```

The plugin should not become a second sync engine. It reads and modifies the same local Android vault that Syncthing-Fork synchronizes.

The plugin connection is considered foreground and opportunistic. Android may suspend it when Obsidian is closed.

## Interaction Control Plane integration

The Obsidian integration should use Work Items and Active Frames from [`INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md`](INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md).

```python
ObsidianActiveFrame(
    logical_vault_id="obsvault_...",
    android_device_id="stdev_...",
    active_note_id="obnote_...",
    active_path="Projects/Friday.md",
    active_revision="sha256:...",
    selected_candidate_set_id="candset_...",
    last_operation_id="obsop_...",
    delivery_state="android_received",
)
```

This supports follow-ups:

```text
Открой второй.
Добавь это туда.
Перемести её в архив.
Теперь свяжи с тем документом.
Поставь эти пункты задачами.
```

## Recommended Playbooks

### ConnectAndroidObsidianVault

```text
create onboarding session
    -> provision per-user Syncthing profile
    -> show server Device ID QR
    -> detect Android device
    -> add device
    -> offer vault folder
    -> wait for folder acceptance
    -> run initial synchronization
    -> collect Android vault alias
    -> verify round trip
    -> mark ready
```

### CaptureConversationToObsidian

```text
select conversation range
    -> summarize
    -> resolve vault and destination
    -> resolve template or capture format
    -> create or update note
    -> verify server revision
    -> observe Android delivery within bounded deadline
    -> return Open in Obsidian action
```

### AppendToDailyNote

```text
resolve local date and vault convention
    -> resolve note path
    -> format capture or task
    -> append idempotently
    -> observe sync state
```

### SearchAndOpenObsidianNote

```text
parse query and vault scope
    -> search exact, lexical, property, graph, and semantic lanes
    -> rank and deduplicate
    -> select or ask user
    -> generate exact Obsidian URI
```

### UpdateObsidianMetadata

```text
resolve note
    -> read current frontmatter and revision
    -> validate typed mutations
    -> atomically update
    -> observe sync state
```

### LinkFridayObjectToObsidian

```text
resolve Friday object
    -> resolve Obsidian note
    -> create durable binding
    -> update property or managed links region
    -> observe sync state
    -> return both navigation targets
```

### ExportFridayResearchToObsidian

```text
resolve completed Work Item outcome
    -> choose template
    -> render claims, sources, and uncertainties
    -> create note
    -> update links
    -> observe sync state
    -> return mobile open action
```

### ResolveSyncthingConflict

```text
identify canonical and conflict copies
    -> compare revisions
    -> generate structured diff
    -> offer keep Android, keep Friday, or merge
    -> apply chosen resolution
    -> remove conflict artifact only after confirmation
    -> rescan and verify
```

## Operation durability

Every mutation is stored before dispatch:

```text
operation_id
work_item_id
vault_id
note identity
method
arguments digest
expected revision
status
attempt
server result revision
delivery state
created_at
updated_at
```

State machine:

```text
prepared
    -> server_write_committed
    -> server_scan_complete
    -> android_delivery_pending
    -> android_received
    -> completed

error branches:
    conflict
    uncertain
    failed
    cancelled
```

Retry rules:

- reads may retry;
- creates reconcile by target path and operation marker;
- append uses an idempotency marker or postcondition;
- full replacement requires the same expected revision;
- move checks source and destination;
- delete never blindly retries after an uncertain outcome;
- offline Android delivery does not roll back a committed server write.

## Suggested storage projection

```text
obsidian_sync_profiles
    id
    friday_user_id
    config_root
    database_root
    api_endpoint
    server_device_id
    runtime_version
    state
    created_at
    updated_at

obsidian_android_devices
    id
    friday_user_id
    sync_profile_id
    syncthing_device_id
    display_name
    state
    last_seen_at
    created_at
    updated_at

obsidian_logical_vaults
    id
    friday_user_id
    sync_profile_id
    android_device_id
    folder_id
    display_name
    server_path
    android_vault_name
    android_path_hint
    convention_json
    state
    created_at
    updated_at

obsidian_onboarding_sessions
    id
    friday_user_id
    state
    setup_token_digest
    expires_at
    error_code
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
    status
    server_revision nullable
    delivery_state
    result_json
    created_at
    updated_at

obsidian_sync_conflicts
    id
    vault_id
    canonical_path
    conflict_path
    state
    detected_at
    resolved_at nullable

obsidian_index_state
    vault_id
    note_binding_id nullable
    path
    source_revision
    embedding_revision nullable
    indexed_at
    deleted_at nullable
```

## Friday Organ implementation

Recommended repository structure:

```text
friday/organs/obsidian/
    __init__.py
    contracts.py
    models.py
    service.py
    router.py
    worker.py

    syncthing_process.py
    syncthing_rest.py
    syncthing_events.py
    syncthing_pairing.py
    syncthing_status.py

    note_store.py
    note_identity.py
    frontmatter.py
    markdown_links.py
    tasks.py
    templates.py
    daily_notes.py
    bases.py
    note_merge.py
    indexer.py

    tools.py
    playbooks.py
    diagnostics.py
```

Obsidian is an appropriate Friday Organ because it is optional and contributes:

```text
capabilities
tools
workers
HTTP and Mini App routes
background process supervision
```

The Friday Organ Protocol should gain a first-class `tools()` extension rather than placing Obsidian branching in `AgentRuntime`.

## API surface

Suggested endpoints:

```text
POST   /api/obsidian/onboarding/start
GET    /api/obsidian/onboarding/{id}
POST   /api/obsidian/onboarding/{id}/cancel
POST   /api/obsidian/onboarding/{id}/confirm-vault
POST   /api/obsidian/onboarding/{id}/verify

GET    /api/obsidian/status
POST   /api/obsidian/test-sync
POST   /api/obsidian/reconnect
POST   /api/obsidian/disconnect

GET    /api/obsidian/vaults
PATCH  /api/obsidian/vaults/{id}

GET    /api/obsidian/notes/search
GET    /api/obsidian/notes/read
POST   /api/obsidian/operations
GET    /api/obsidian/operations/{id}

GET    /api/obsidian/conflicts
POST   /api/obsidian/conflicts/{id}/resolve

POST   /api/obsidian/index/rebuild
GET    /api/obsidian/index/status
```

The conversational runtime calls the service directly through Execution Kernel handlers instead of calling its own HTTP API.

## Diagnostics

Add an Obsidian and Syncthing section to Friday diagnostics:

```text
connected Friday users
managed Syncthing processes
Syncthing runtime versions
server Device IDs by profile
Android peer state
last device contact
folder acceptance state
server folder health
remote completion percentage
pending bytes and items
folder errors
conflict count
last successful operation
pending delivery operations
index coverage
```

User-facing doctor output:

```text
Obsidian vault: Friday
Server checkout: healthy
Syncthing process: running
Android device: Pixel 10
Phone connection: offline
Pending delivery: 2 files
Last phone contact: 18 minutes ago
Conflicts: 0
```

## MCP position

MCP is not needed inside this integration.

Preferred path:

```text
Friday-owned tools
    -> ObsidianService
    -> note store and Syncthing adapter
```

An optional MCP façade may later expose the same stable Friday capabilities to external agents.

A generic Obsidian MCP server does not replace the Android synchronization transport, device pairing, note identity, Work Items, or delivery-state model.

## Implementation phases

### P0: freeze the free Android product contract

- make Android-only and no-subscription assumptions executable;
- remove Obsidian account and Headless concepts from the primary schema;
- define the sync truth model;
- define the per-user Syncthing process contract;
- define onboarding and delivery states;
- select a tested Syncthing and Syncthing-Fork version range.

Deliverable: a versioned architecture contract and probe utility.

### P1: managed Syncthing runtime

- package or require Syncthing on the Friday host;
- create per-user config and database roots;
- supervise one process per connected user;
- implement REST authentication and version probe;
- implement device, folder, scan, completion, and event adapters;
- add diagnostics and restart recovery.

Deliverable: Friday can create and monitor one isolated server-side Syncthing profile.

### P2: Telegram onboarding

- add `/obsidian` status panel;
- add Mini App and normal HTTPS fallback;
- provision profile, folder, and Device ID QR;
- detect and accept the Android device;
- offer the folder;
- wait for Android acceptance;
- collect Android vault alias;
- perform round-trip verification;
- make onboarding resumable and idempotent.

Deliverable: one Android user reaches `ready` without an Obsidian account or desktop.

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

Add atomic writes, revision checks, and an operation ledger.

Deliverable: the user performs useful background Obsidian work through Telegram.

### P4: delivery semantics and conflict handling

- track server write, scan, peer, and remote completion separately;
- return pending delivery without blocking;
- detect `sync-conflict` files;
- add compare and resolution flow;
- enable server-side versioning;
- add reconnect reconciliation.

Deliverable: offline Android and concurrent edits do not produce false success or silent loss.

### P5: note identity, links, and graph

- add stable note IDs and bindings;
- build wikilink and Markdown-link index;
- implement backlinks, unresolved links, orphans, and dead ends;
- implement move with bounded link updates;
- preserve identity across rename and move.

Deliverable: Friday can organize a real vault rather than only append files.

### P6: semantic indexing and Interaction Control Plane

- index configured vault roots;
- combine lexical, property, graph, and semantic lanes;
- add Obsidian Active Frame;
- persist candidate sets;
- implement Playbooks and Completion Gates;
- combine Obsidian notes with Friday documents and conversations.

Deliverable: approximate search and short follow-ups work across the integrated system.

### P7: tasks, Bases, and advanced note semantics

- durable task targeting;
- typed BaseSpec and `.base` generation;
- managed regions;
- Friday-to-Obsidian object bindings;
- explicit Obsidian-note ingestion into Friday Inbox.

Deliverable: broader Obsidian workflows are available without desktop Obsidian.

### P8: optional Android companion plugin

- pair plugin with the existing Friday user and logical vault;
- expose current note and selection;
- insert at cursor;
- run native commands;
- open panes and views;
- keep the plugin out of the sync path.

Deliverable: live in-app context when Obsidian is open.

### P9: optional future transports

- pooled Syncthing daemon for larger deployments;
- alternative free sync providers;
- desktop CLI adapter;
- paid-provider adapter if ever desired;
- MCP façade;
- packaged mobile companion plugin release.

## Suggested first release scope

The first production-useful release should contain:

```text
one Friday user
one Android device
one logical vault
per-user Syncthing process
Telegram /obsidian onboarding
QR device pairing
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

Do not block this release on:

```text
companion plugin
community-plugin commands
custom Bases view
Canvas
multiple Android devices
multiple vaults per user
pooled Syncthing runtime
```

## Acceptance criteria

### Free Android onboarding

- A user with no Obsidian account can complete setup.
- `/obsidian` begins the setup from a verified Friday identity.
- Mini App and ordinary HTTPS flows resume the same session.
- Friday provisions exactly one server Syncthing profile for one accepted setup.
- The QR contains the correct per-user server Device ID.
- The first Android pending device is bound to the correct user profile.
- Friday offers exactly one logical vault folder.
- Setup waits until the Android client shares the folder back.
- A new Android folder can be opened as an Obsidian vault.
- The round-trip note reaches 100 percent remote completion while connected.
- Refreshing or restarting Friday does not duplicate profiles, devices, or folders.

### Identity

- Friday never requires an Obsidian email or password.
- Friday user, Syncthing profile, Android device, logical vault, and note are separate identities.
- A device reconnect preserves its binding.
- Replacing the phone requires an explicit new-device flow.
- Rename and move preserve bound note identity.

### Android operation

- Friday may write while the phone is offline.
- Offline delivery remains pending rather than failed.
- Reconnection transfers pending files.
- Friday distinguishes server write from Android receipt.
- Friday never claims that Obsidian opened a note without a user or plugin action.
- The Open in Obsidian link uses the configured Android vault alias.

### Notes

- Friday creates, reads, appends, prepends, replaces, moves, and deletes notes.
- Every full replacement uses an expected revision.
- Typed property updates preserve the note body.
- Daily-note paths follow the stored vault convention.
- Template creation preserves unknown template syntax.
- Managed-region updates preserve user text outside the region.

### Sync and conflicts

- Syncthing process restart does not lose pairing state.
- Server scan and remote completion are observable.
- A concurrent edit creates a visible conflict workflow.
- Conflict copies are never deleted automatically.
- Server-side versioning retains configured remote replacements and deletions.
- An uncertain mutation is reconciled before retry.
- One accepted operation produces at most one durable note mutation.

### Search and graph

- Exact path and title search work over the server checkout.
- Lexical search returns path and excerpt.
- Indexed notes can be found by approximate semantic description.
- Backlinks and outgoing links are computed without a running desktop app.
- A move with `update_links=true` reports all changed and unresolved references.
- Friday projections are not re-ingested as independent evidence.

### Companion plugin independence

- All core synchronized note operations work with no plugin installed.
- Plugin offline state does not mark the vault unhealthy.
- Installing the plugin adds active-note capabilities without changing the sync binding.

## Suggested executable regression tests

```text
test_free_android_setup_requires_no_obsidian_account.py
test_obsidian_setup_starts_from_the_friday_telegram_identity.py
test_one_setup_creates_one_syncthing_profile.py
test_each_user_profile_has_a_unique_server_device_id.py
test_the_first_pending_android_device_binds_to_the_correct_profile.py
test_folder_acceptance_is_required_before_ready.py
test_round_trip_verification_reaches_remote_completion.py
test_refreshing_setup_does_not_duplicate_the_folder.py

test_friday_can_write_while_android_is_offline.py
test_offline_delivery_is_pending_not_failed.py
test_android_receipt_is_distinct_from_server_write.py
test_friday_never_claims_that_mobile_obsidian_opened_without_evidence.py
test_the_open_uri_uses_the_configured_android_vault_alias.py

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
test_remote_completion_updates_the_operation_delivery_state.py

test_native_and_semantic_note_results_deduplicate.py
test_android_originated_changes_reindex_only_the_changed_note.py
test_delete_events_remove_note_passages.py
test_a_friday_projection_is_not_reingested_as_new_knowledge.py
test_the_second_result_uses_the_active_candidate_set.py
test_add_that_there_uses_the_active_note.py

test_core_vault_operations_need_no_companion_plugin.py
test_companion_plugin_offline_does_not_break_syncthing_operations.py
test_obsidian_tools_are_registered_by_the_organ_not_agent_runtime.py
```

## Key architectural invariants

1. The primary free Android path requires no Obsidian account.
2. Friday binds to an Android Syncthing device and logical vault, not to cloud identity.
3. Syncthing-Fork is the persistent transport between Friday and Android.
4. Obsidian URI is navigation and handoff, not synchronization proof.
5. The companion plugin is optional foreground context, not the sync backbone.
6. Friday owns the natural-language task and Work Item.
7. Obsidian owns native mobile viewing and editing.
8. The server checkout is Friday's operational copy of the vault.
9. Local write, server scan, Android receipt, and Obsidian open are separate postconditions.
10. Path is navigation, not durable note identity after binding.
11. A projection is rebuildable and never treated as independent evidence.
12. A model may select and parameterize capabilities, but it never writes vault paths directly.
13. One accepted operation produces at most one durable mutation.
14. Offline Android delivery never erases a committed server result.
15. Conflicts preserve both versions until an explicit resolution.
16. Core note features do not depend on a running Obsidian desktop or mobile process.
17. The sync provider is replaceable behind Friday-owned contracts.

## Final recommendation

Build the integration in this order:

```text
per-user Syncthing runtime
    -> Telegram QR onboarding
    -> Android folder acceptance
    -> round-trip verification
    -> native server note operations
    -> precise delivery status
    -> conflict handling
    -> note identity and link graph
    -> semantic indexing and Work Items
    -> optional companion plugin
```

The existing `MemoryVault` remains a rebuildable Friday projection. The new Obsidian Organ operates ordinary user vaults through a managed server checkout and Syncthing-Fork.

The final free Android experience is:

```text
The user installs Obsidian and Syncthing-Fork once.
The user scans one Friday QR code and accepts one folder.
The user opens that folder as an Obsidian vault.
After that, the user talks to Friday in Telegram.
Friday searches, creates, edits, links, and organizes notes.
Syncthing-Fork transfers changes whenever Android is available.
Friday reports whether delivery is complete or still pending.
A one-tap URI opens the result in the official Obsidian app.
```

This is not as frictionless as a central account-based sync service, but it is achievable without a subscription, without a desktop, and without building a complete proprietary synchronization engine inside Friday.

## Official and project references

Sources checked on 21 August 2026:

- [Obsidian: Sync your notes across devices](https://obsidian.md/help/sync-notes)
- [Obsidian for Android](https://obsidian.md/help/android)
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
- [Syncthing-Fork source](https://github.com/researchxxl/syncthing-android)
