# Friday Obsidian Integration Acceptance Battery

> Document ID: FRIDAY-OBS-TEST-001  
> Status: active acceptance specification; results and remaining work are owned
> by [`PROJECT_BACKLOG.md`](PROJECT_BACKLOG.md)
> Repository snapshot: `main`, Friday `0.207.4`, 22 August 2026  
> Architecture contract under test: [`FRIDAY-OBS-001 v0.4`](OBSIDIAN_INTEGRATION_ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md)  
> Primary scenario: one Android phone, Telegram as the Friday interface, free Obsidian, Syncthing-Fork, no Obsidian account, no Obsidian Sync, no desktop requirement, and no companion plugin for the core battery.

## Purpose

This document defines the end-to-end acceptance battery for the free Android Obsidian integration.

The tests are written as realistic user episodes rather than isolated API calls. Each case contains:

- the exact Russian-language request sent to Friday;
- any required fixture or device action;
- the expected Friday-visible outcome;
- the expected server-vault result;
- the expected Syncthing and Android result;
- explicit failure conditions.

The battery is intended to answer one product question:

> Can a person use Obsidian through Friday from one Android phone without losing work, duplicating effects, or being misled about what completed?

## Truth model

Every mutating scenario must distinguish four different postconditions:

```text
1. server_committed
   The intended file revision exists in Friday's server-side vault checkout.

2. server_scan_complete
   The managed Syncthing profile has scanned and indexed that revision.

3. android_delivered
   Syncthing reports that the paired Android device has received the revision.

4. obsidian_open_confirmed
   The note was opened by an explicit user action or observable companion-plugin action.
```

These states must never be collapsed into one generic `done` flag.

Examples of acceptable Friday responses:

```text
The note was saved in the server vault. Delivery to Android is pending because the phone is offline.
```

```text
The note was saved and Syncthing reports delivery to Android. Use the button below to open it in Obsidian.
```

An unacceptable response is:

```text
The note is already open on your phone.
```

unless Friday has direct evidence of that event.

## Test environment

Use a dedicated test vault and test user.

```text
Friday user: obsidian-test-user
Telegram chat: dedicated test chat
Android vault name: Friday-Test
Server logical vault: Friday-Test
Android vault path: device-storage folder selected during onboarding
Vault convention:
    notes folder: Notes
    projects folder: Projects
    daily folder: Daily
    templates folder: Templates
    attachments folder: Attachments
    daily filename: YYYY-MM-DD.md
Time zone: Europe/Berlin
Syncthing folder type: Send & Receive
Companion plugin: not installed for the core battery
```

The test vault must not contain production notes.

Before a clean run:

1. remove or archive the previous `Friday-Test` server checkout;
2. clear stale test note bindings and semantic passages;
3. verify that no unresolved operation from an earlier run remains active;
4. verify that Syncthing reports the expected Android device and folder;
5. record the current local date and time as `T0`;
6. enable the diagnostic view for operation ID, file revision, scan state, and Android delivery state.

## Evidence to capture

For each test, retain:

```text
user request
Friday response
Work Item ID when applicable
Obsidian operation ID
resolved vault and path
pre-write revision
post-write revision
server commit timestamp
server scan state
Android delivery state
relevant note body or diff
search result IDs and ordering
failure or recovery events
```

Screenshots are useful for the Android side, but the release decision must not depend only on screenshots. Friday-side state and exact vault contents are the authoritative test evidence.

## Global pass criteria

The complete battery fails if any scenario produces:

- silent loss of user-authored text;
- duplicate durable effects from one accepted operation;
- a false claim that Android delivery completed;
- a false claim that Obsidian opened a note;
- cross-user or cross-vault targeting;
- stale search results presented as live notes after deletion;
- an unresolved ambiguous reference executed against an arbitrary candidate;
- disappearance of either side of a synchronization conflict;
- re-ingestion of a Friday projection as independent knowledge;
- dependence on a mobile companion plugin for a core operation.

## Release tiers

### Tier A: minimal smoke gate

The following seven scenarios must pass before a build is considered usable:

1. Create a note.
2. Append to the daily note.
3. Find a note by paraphrased content.
4. Continue with “open the second one” and then “add it there”.
5. Create a note on Android and find it through Friday.
6. Write while Android is offline and later deliver the revision.
7. Produce a real concurrent-edit conflict and preserve both versions.

### Tier B: core release gate

All onboarding and scenarios 1 through 18 must pass.

### Tier C: optional foreground extension

Companion-plugin tests are a separate gate. Failure there must not invalidate the Syncthing-backed core unless plugin support is advertised as released.

---

# Onboarding acceptance

## OBS-ONB-01: complete setup on one Android phone

### Preconditions

- The Friday user has no existing Obsidian binding.
- Obsidian and Syncthing-Fork are installed on one Android phone.
- No second screen is available.
- No Obsidian account exists or is required.

### User action

Send to Friday:

```text
/obsidian
```

Then complete the displayed steps:

```text
1. Tap “Copy Friday Device ID”.
2. Open Syncthing-Fork.
3. Open Devices and choose Add device.
4. Paste the copied ID and save it as Friday.
5. Accept the offered Friday-Test folder.
6. Select a device-storage folder.
7. Open that folder as the Friday-Test vault in Obsidian.
8. Tap Friday's verification link.
```

### Expected Friday outcome

- One resumable onboarding session is created.
- The complete server Device ID is copied through Telegram when supported.
- The same complete ID is also displayed as selectable text.
- QR is optional and never required.
- Friday observes the Android pending device after the user saves the pasted server ID.
- Friday records the Android Device ID automatically.
- The user is never asked to copy the Android Device ID back into Telegram.
- Friday offers exactly one logical vault folder.
- The onboarding state reaches `ready` only after folder acceptance and round-trip verification.

### Expected server result

- Exactly one Syncthing profile exists for the Friday user.
- Exactly one server Device ID exists for that profile.
- Exactly one logical vault and folder ID exist.
- Reopening `/obsidian` or refreshing the setup page does not duplicate any of them.

### Expected Android result

- Syncthing-Fork shows Friday as a paired device.
- The Friday-Test folder is `Send & Receive`.
- Obsidian can open the selected folder as a vault.
- The connection-test note arrives and opens through the provided URI.

### Fail if

- setup requires scanning a QR displayed on the same phone;
- an Obsidian email, password, or subscription is requested;
- the user must return the Android Device ID manually;
- repeated setup creates duplicate devices or folders;
- Friday reports `ready` before folder acceptance or round-trip verification.

---

# Core note operations

## OBS-NOTE-01: create a simple note

### User message

```text
Создай в Obsidian заметку `Projects/Friday Test.md`. Заголовок: «Тест интеграции Friday». Внутри напиши, что заметка создана через Telegram, и добавь текущую дату.
```

### Expected Friday outcome

- Friday resolves the `Friday-Test` logical vault.
- The operation returns the exact path `Projects/Friday Test.md`.
- The response identifies the local date used.
- The response contains an `Open in Obsidian` action.
- The response reports server commit and Android delivery separately.

### Expected vault result

- One file exists at `Projects/Friday Test.md`.
- It contains valid Markdown.
- Its heading is `Тест интеграции Friday`.
- It states that the note was created through Telegram.
- It contains the correct date for `Europe/Berlin` at execution time.
- No second similarly named note is created.

### Expected Android result

- After Syncthing delivery, the file appears in the Android vault.
- The open action targets the `Friday-Test` vault and exact note.

### Fail if

- Friday creates the note in another vault or folder;
- the reported date uses the wrong local day;
- Friday says the phone received the file before Syncthing confirms it;
- the open action targets an ambiguous title rather than the exact path.

## OBS-NOTE-02: append without destroying existing content

### User message

```text
Добавь в конец заметки `Projects/Friday Test.md` раздел «Проверка дополнения» и одну строку: «Этот текст был добавлен отдельной командой».
```

### Expected result

- All previous content remains unchanged.
- One new section named `Проверка дополнения` is appended.
- The requested line appears exactly once for the accepted operation.
- Existing frontmatter remains valid.
- The operation returns the new revision digest.
- A transport retry of the same operation ID does not append the section again.

### Fail if

- Friday regenerates or summarizes the complete note;
- earlier text disappears or changes unexpectedly;
- one operation produces duplicate appended sections.

## OBS-DAILY-01: append to today's daily note

### User message

```text
Добавь в сегодняшнюю ежедневную заметку раздел «Friday» и пункт: «Проверена интеграция с Obsidian».
```

### Expected result

- Friday resolves today's date in `Europe/Berlin`.
- The configured daily path is used, for example `Daily/YYYY-MM-DD.md`.
- The note is created if absent.
- A `Friday` section is reused if already present rather than duplicated.
- The requested item is added once for one operation.
- A retry after an uncertain transport result reconciles the postcondition before writing again.

### Fail if

- UTC causes the previous or next day's note to be selected;
- duplicate `Friday` headings are created;
- recovery creates two identical items.

## OBS-TASK-01: create and retrieve a dated task

### User message

```text
Добавь в сегодняшнюю заметку задачу проверить поиск в Obsidian завтра в 10 утра.
```

### Expected result

- A standard Markdown task is added to today's daily note.
- “Tomorrow” is resolved to a concrete local date.
- `10:00` is represented according to the configured task convention.
- Friday reports the note and section where the task was added.

Then send:

```text
Покажи незавершённые задачи про Obsidian.
```

Expected:

- The new task is returned.
- Its source path and concrete date are shown.
- Completed tasks with similar text are not returned as incomplete.

## OBS-META-01: update typed properties and tags

### User message

```text
У заметки `Projects/Friday Test.md` поставь статус `review`, проект `Friday` и добавь теги `integration`, `obsidian` и `test`.
```

### Expected frontmatter

A semantically equivalent result to:

```yaml
---
status: review
project: Friday
tags:
  - integration
  - obsidian
  - test
---
```

### Expected invariants

- Existing unrelated properties remain present.
- The Markdown body is unchanged.
- Re-adding `obsidian` does not create a duplicate tag.
- Tags remain a list, not one serialized text blob.
- The update is atomic from Friday's point of view.

### Fail if

- the body is rewritten;
- valid existing YAML disappears;
- a property update corrupts frontmatter boundaries.

---

# Search and operational-memory acceptance

## OBS-SEARCH-01: find a note by paraphrased content

### Fixture created on Android

Create `Projects/Retrieval Problem.md` with:

```text
Старые документы иногда исчезали из семантической выдачи, потому что набор кандидатов ограничивался сравнительно свежими объектами.
```

Wait until Friday observes and indexes the Android-originated revision.

### User message

```text
Найди в Obsidian заметку, где мы обсуждали, что старые файлы не попадали в поиск из-за слишком маленького списка кандидатов.
```

### Expected result

- `Projects/Retrieval Problem.md` is the highest-ranked or clearly leading candidate.
- Friday returns a relevant excerpt.
- The match may be labeled semantic or paraphrased rather than exact.
- A lexically noisy but irrelevant Friday note does not outrank it.
- If semantic index coverage is incomplete, Friday reports partial coverage rather than asserting that no note exists.

### Fail if

- exact wording is required;
- a stale or unrelated note is selected solely because it contains more shared tokens;
- Friday falsely reports absence while the note is known but not fully indexed.

## OBS-SEARCH-02: combine approximate date and approximate content

### Fixture

Add this property to `Projects/Retrieval Problem.md`:

```yaml
created: 2026-08-04
```

Wait for the updated revision to be indexed.

### User message

```text
Найди заметку про проблемы поиска, которую я делал примерно в начале августа 2026 года.
```

### Expected result

- Friday resolves an approximate early-August 2026 temporal constraint.
- Both content and date contribute to ranking.
- The target note is returned without requiring the exact date.
- If several candidates remain plausible, Friday presents them instead of selecting one arbitrarily.
- The response identifies which date property was used.

### Fail if

- “approximately” becomes an exact one-day SQL filter;
- a mentioned date inside unrelated note text is silently treated as the note creation date;
- Friday hides real ambiguity.

## OBS-CONT-01: continue from a stable candidate set

### User message 1

```text
Найди все заметки про Friday и поиск.
```

Record the ordered candidate set returned by Friday.

### User message 2

```text
Открой вторую.
```

### User message 3

```text
Добавь туда раздел «Следующие шаги» и пункт про проверку семантического индекса.
```

### Expected result

- “The second one” resolves against the persisted candidate set, not a fresh search.
- Friday opens the exact second candidate from that set.
- “There” resolves to the same selected note.
- The update targets the stored note identity and observed revision.
- If the candidate set expired or the target was deleted, Friday refuses silent reuse and re-resolves explicitly.

### Fail if

- a new search changes the ordering before selection;
- the third request modifies another note with a similar title;
- the model reconstructs the target only from conversation prose when a durable candidate ID exists.

## OBS-SYNC-01: index an Android-originated note

### Android action

Create `Mobile/Created On Phone.md` in mobile Obsidian:

```text
Эту заметку создали непосредственно в мобильном Obsidian. В ней упоминается фиолетовый маршрутизатор и тест обратной синхронизации.
```

Wait until Syncthing delivers the file to the server checkout.

### User message

```text
Найди заметку про фиолетовый маршрутизатор.
```

### Expected result

- Friday finds `Mobile/Created On Phone.md`.
- Only the changed note needs incremental reindexing.
- Friday can read the note and answer questions about its content.
- The note is identified as Android-originated or ordinary user-owned content, not as a Friday projection.

### Fail if

- Friday only indexes notes it created itself;
- a full-vault rebuild is required for every phone edit;
- the note is promoted into Friday knowledge merely because it synchronized.

---

# Links, moves, templates, and structured views

## OBS-LINK-01: compute backlinks without a running Obsidian app

### Fixture

Create:

```text
Projects/Friday.md
Notes/Search.md
Notes/Obsidian.md
```

Place this link in `Notes/Search.md` and `Notes/Obsidian.md`:

```markdown
[[Projects/Friday]]
```

### User message

```text
Какие заметки ссылаются на `Projects/Friday`?
```

### Expected result

- Exactly the two linking notes are returned.
- Each result includes its path.
- Backlinks are distinguished from outgoing links.
- A plain text mention without wikilink syntax is not counted as a resolved wikilink.
- No running mobile or desktop Obsidian process is required.

## OBS-MOVE-01: move a note and update resolvable links

### User message

```text
Перемести `Projects/Friday.md` в `Architecture/Friday.md` и обнови ссылки на неё.
```

### Expected result

- The file moves to `Architecture/Friday.md`.
- Its stable integration identity remains unchanged.
- Links in `Notes/Search.md` and `Notes/Obsidian.md` are updated.
- No stale duplicate remains at the old path.
- Friday reports every changed note.
- Ambiguous, dynamic, or unresolved references are listed rather than rewritten by guesswork.

Then send:

```text
Какие заметки теперь ссылаются на архитектуру Friday?
```

Expected:

- The same two source notes are returned against the new target path.

### Fail if

- move creates a copy and leaves the original;
- note identity changes;
- Friday updates only its index but not the actual source links;
- ambiguous links are silently rewritten.

## OBS-TEMPLATE-01: create a note from a template

### Fixture

Create `Templates/Meeting.md`:

```markdown
---
type: meeting
date: {{date}}
project: {{project}}
---

# {{title}}

## Participants

{{participants}}

## Discussion

{{discussion}}

## Actions

{{actions}}
```

### User message

```text
Создай по шаблону Meeting заметку о проверке интеграции Obsidian. Проект Friday, участники Алиса и Борис. В обсуждение добавь, что базовая синхронизация работает. В действия добавь задачу проверить конфликты.
```

### Expected result

- One new note is created from the selected template.
- The current concrete date is inserted.
- The project, title, participants, discussion, and actions are filled correctly.
- YAML remains valid.
- Unknown template syntax, if present, is preserved rather than deleted.
- Friday returns the exact path, revision, delivery state, and open action.

### Fail if

- Friday ignores the template structure and writes an unrelated free-form note;
- required supplied fields remain unresolved;
- frontmatter becomes invalid.

## OBS-WORK-01: save a structured conversation outcome

### User message

```text
Сохрани краткие итоги нашего текущего разговора в Obsidian. Создай заметку `Research/Conversation Summary.md`, отдельно укажи выводы, нерешённые вопросы и следующие действия.
```

### Expected structure

```markdown
# Conversation Summary

## Conclusions

...

## Open questions

...

## Next actions

...
```

### Expected invariants

- Friday uses the current conversation scope.
- Internal tool traces and hidden orchestration details are absent.
- Unfinished actions are not presented as completed facts.
- Genuine unresolved questions remain under `Open questions`.
- The note is synchronized normally.

Then send:

```text
Добавь туда ссылки на заметки, которые мы сегодня использовали.
```

Expected:

- “There” resolves to `Research/Conversation Summary.md` through the active Work Item.
- Relevant note links are added without replacing the existing body.

## OBS-BASE-01: create and evaluate a Base

### Fixture

Create several notes with:

```yaml
project: Friday
status: active
```

and one with:

```yaml
project: Friday
status: done
```

### User message

```text
Создай Base `Friday Active Notes`, который показывает заметки проекта Friday со статусом не `done`. Выведи название, статус и дату изменения.
```

### Expected result

- A supported `.base` file is created.
- Friday's server-side `BaseSpec` evaluator returns the same intended collection.
- Active Friday notes are included.
- The `done` note is excluded.
- The requested columns and sort semantics are represented.
- Changing a note from `active` to `done` removes it from the next query result after reindexing.

### Fail if

- Friday claims to have queried a native running Obsidian engine when none exists;
- the generated Base and Friday evaluator use incompatible filters;
- stale rows remain indefinitely after property changes.

---

# Synchronization, recovery, and lifecycle acceptance

## OBS-OFFLINE-01: write while Android is offline

### Setup

Stop Syncthing-Fork or disconnect the Android device from the network.

### User message

```text
Создай заметку `Offline/Pending Delivery.md` и напиши, что она была создана, пока телефон был offline.
```

### Expected immediate result

Friday reports semantically equivalent states:

```text
Server vault: saved
Server scan: complete or pending
Android delivery: pending because the device is offline
```

### Expected recovery result

After Syncthing-Fork reconnects:

- the existing server revision is delivered;
- the operation transitions to `android_delivered`;
- no second note is created;
- `/obsidian` reports current delivery state;
- the open action works after delivery.

### Fail if

- offline delivery is reported as failed permanently;
- Friday claims phone receipt before evidence;
- reconnection re-executes note creation.

## OBS-CONFLICT-01: preserve both sides of a concurrent edit

### Setup

1. Ensure `Projects/Friday Test.md` is synchronized.
2. Disconnect Android from Syncthing.
3. Edit the note on Android.
4. While Android is offline, send Friday the request below.

### User message

```text
Замени раздел «Проверка дополнения» текстом: «Версия, записанная Friday».
```

Then reconnect Syncthing-Fork.

### Expected result

- Neither Android nor Friday content is silently discarded.
- A Syncthing conflict artifact or equivalent explicit conflict record appears.
- Friday surfaces the conflict to the user.
- Both revisions remain accessible.
- The conflict file is never deleted automatically.

Then send:

```text
Покажи различия и собери объединённую версию, сохранив оба изменения.
```

Expected:

- Friday presents or produces a merge preview.
- No canonical side is overwritten before explicit acceptance.
- After acceptance, the merged revision contains both intended changes.

### Fail if

- last writer silently wins;
- conflict detection is hidden;
- Friday deletes the conflict artifact before resolution.

## OBS-RECOVERY-01: resume an interrupted append without duplication

### User message

```text
Добавь в ежедневную заметку строку «Проверка идемпотентности».
```

During execution, restart the Friday backend or managed Syncthing process after dispatch but before final acknowledgement.

After recovery, send:

```text
Продолжай предыдущую задачу.
```

### Expected result

- Friday resumes the same Work Item.
- It reconciles the note postcondition before retrying.
- The requested line exists exactly once.
- The original operation ID or idempotency identity remains traceable.
- Completion is not declared until the server file state is verified.

### Fail if

- a second daily note is created;
- the line is duplicated;
- Friday loses the active work and starts an unrelated task;
- a transport error is presented as successful completion.

## OBS-DELETE-01: delete a note and close its search lifecycle

### Fixture

Create and index:

```text
Scratch/Delete Me.md
```

Optionally create another note that links to it.

### User message

```text
Удали тестовую заметку `Scratch/Delete Me.md`.
```

### Expected result

- Friday resolves one exact target.
- Any configured confirmation step is applied.
- The server file is removed once.
- The deletion is synchronized to Android.
- Lexical and semantic note passages are invalidated.
- Backlinks to the deleted target become unresolved.
- An Active Frame pointing to the deleted note is invalidated.

Then send:

```text
Найди заметку Delete Me.
```

Expected response should be semantically equivalent to:

```text
No active note was found. A previously known note with that identity was deleted.
```

### Fail if

- a stale semantic passage is returned as a live note;
- the old Active Frame still permits edits;
- Friday silently recreates the note during recovery;
- backlinks remain falsely resolved.

---

# Optional companion-plugin extension battery

These tests are not part of the core Syncthing release gate.

## OBS-PLUGIN-01: use the current note and selection

### Android action

Open a note in Obsidian, select one paragraph, and invoke the Friday companion action:

```text
Explain the selected text with Friday.
```

### Expected result

- The plugin sends the exact vault, path, revision, and selection.
- Friday answers about the selection rather than an older active note.
- No full-vault rescan is required.
- Closing Obsidian or disconnecting the plugin does not break normal Syncthing operations.

## OBS-PLUGIN-02: insert at the current cursor

### User or plugin request

```text
Insert the last Friday answer at the current cursor.
```

### Expected result

- The insertion targets the currently active note and cursor context.
- The operation returns the post-write revision.
- A stale active-note event produces `conflict` or `needs_input`, not a blind write.
- Syncthing later transports the same resulting file normally.

---

# Compact execution checklist

For a rapid release candidate check, execute in this order:

```text
[ ] OBS-ONB-01   one-phone clipboard onboarding
[ ] OBS-NOTE-01  create note
[ ] OBS-DAILY-01 daily-note append
[ ] OBS-SEARCH-01 semantic paraphrase search
[ ] OBS-CONT-01  second-result continuation
[ ] OBS-SYNC-01  Android-originated note
[ ] OBS-OFFLINE-01 offline write and later delivery
[ ] OBS-CONFLICT-01 concurrent conflict preservation
```

This compact gate is still failed by any false completion claim, duplicate durable write, silent data loss, or unresolved arbitrary target selection.

# Suggested automated mapping

The manual battery should be backed by executable tests such as:

```text
test_one_phone_setup_never_requires_qr.py
test_copy_text_button_contains_the_exact_server_device_id.py
test_android_pending_device_is_discovered_after_manual_paste.py
test_user_never_has_to_return_the_android_device_id.py

test_friday_can_create_and_open_an_obsidian_note.py
test_friday_can_append_to_the_daily_note_once.py
test_typed_properties_preserve_markdown_body.py

test_native_and_semantic_results_deduplicate.py
test_the_second_result_uses_the_active_candidate_set.py
test_add_that_there_uses_the_active_note.py
test_android_changes_reindex_only_the_changed_note.py

test_a_note_rename_preserves_the_integration_identity.py
test_a_move_updates_resolvable_links_and_reports_ambiguous_links.py
test_a_friday_projection_is_not_reingested_as_new_knowledge.py

test_friday_can_write_while_android_is_offline.py
test_offline_delivery_is_pending_not_failed.py
test_android_receipt_is_distinct_from_server_write.py
test_an_uncertain_append_is_reconciled_before_retry.py
test_a_sync_conflict_becomes_a_user_visible_conflict_record.py
test_conflict_files_are_not_deleted_without_resolution.py
test_delete_events_remove_note_passages.py

test_core_operations_need_no_companion_plugin.py
test_companion_plugin_offline_does_not_break_syncthing_operations.py
```

# Release decision

The integration is ready for ordinary use only when:

1. the one-phone onboarding path succeeds without QR or Obsidian credentials;
2. the Tier A smoke gate passes repeatedly;
3. all Tier B cases pass at least once against a real Android device;
4. no case produces silent loss, duplicate effects, false delivery, or stale active-note behavior;
5. failures remain localized and resumable through Work Items and the operation ledger;
6. the same core functionality works with no companion plugin installed.

A passing implementation is not merely a Markdown generator.

It is a bidirectional, searchable, resumable Obsidian workspace in which Friday can act while Android is offline, correctly report delivery state, accept edits originating on the phone, preserve conflicts, and continue natural-language work across multiple turns.
