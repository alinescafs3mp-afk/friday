# Jericho Organ Protocol (JOP)

An **Organ** is a self-contained extension module for Jericho — a plugin. This
document is the contract: read it to understand how to write, integrate, or
review an Organ. It is written to be followed without any other context.

## What an Organ is

An Organ is a Python package under `jericho/organs/<name>/` that contributes one
or more of a small, fixed set of **extension points**. It is composed into the
running system at startup by the `OrganRegistry`. There is no dynamic discovery
and no entry-point magic: Organs are listed explicitly in
`jericho.organs.build_registry`, so the running route/worker/capability set is
always exactly what the code says.

The guiding rule, inherited from the whole system:

> **An Organ may initiate communication, but it never turns material into
> canonical knowledge silently.** Reaching out to the user (a reminder, a
> digest, a question) is free; writing to the knowledge graph still goes through
> the review gate like everything else.

## Extension points

An Organ subclasses `jericho.organs.Organ` and overrides only what it needs.
All extension points are optional; the defaults contribute nothing.

```python
from jericho.organs import Organ, OrganWorker, ServiceContext

class MyOrgan(Organ):
    name = "my_organ"
    version = "1.0"

    def capabilities(self):        # -> Sequence[CapabilityDefinition]
        return ()

    def workers(self, ctx):        # -> Sequence[OrganWorker]
        return ()

    def router(self):              # -> APIRouter | None
        return None
```

### `capabilities()`

Return `CapabilityDefinition` objects (from `jericho.permissions`) to add new
entries to the capability model. Set `source="organ"` (or your organ's name) and
use only the built-in preset keys (`owner`/`admin`/`moderator`/`user`/`guest`)
in `default_presets`. They are registered on the `AuthorizationService` at
startup and are enforced exactly like core capabilities.

### `workers(ctx)`

Return `OrganWorker` specs to run background periodic tasks. Each worker's `run`
is an `async def run(ctx: ServiceContext)` coroutine invoked on `interval_sec`
under a timeout, supervised alongside the core workers (an isolated crash never
takes down another worker).

```python
OrganWorker(
    name="my_scan",
    run=my_scan,                 # async def my_scan(ctx): ...
    interval_sec=900.0,
    enabled=ctx.settings.my_feature_enabled,
    run_immediately=False,
    timeout_sec=300.0,
)
```

### `router()`

Return a FastAPI `APIRouter` (conventionally `APIRouter(prefix="/api/...")`) to
add HTTP endpoints. Gate every mutating route with `_require(request, "<cap>")`
against a capability the organ declared, and audit state changes with the
existing `_audit(...)` helper pattern. The router is mounted after the core
routers.

## `ServiceContext`

Runtime code receives a `ServiceContext` — the only surface an Organ is allowed
to touch (never reach into globals):

| field       | what it is                                         |
|-------------|----------------------------------------------------|
| `settings`  | the frozen `JerichoSettings`                       |
| `storage`   | `JerichoStorage` (DB + all data access)            |
| `kg`        | `KnowledgeGraph`                                    |
| `ingestion` | `IngestionPipeline`                                 |
| `llm`       | the `LLMRouter` (may be disabled — check `llm.enabled`) |

Need another service? Add a field here (once), not per-organ.

## Quiet hours

Proactive organs must not ping the user at night. A shared, midnight-safe helper
`jericho.organs.in_quiet_hours(hour, start, end)` and the system-wide settings
`quiet_hours_start`/`quiet_hours_end` (UTC, `start == end` disables) apply to
**every** organ — gate enqueue on them so a due message simply waits until the
window ends.

## Reaching the user: the outbound notification channel

Jericho's backend does not talk to Telegram directly — the Telegram bridge is
the only holder of the bot token. To push a message, an Organ **enqueues** it;
the bridge drains the queue and delivers it.

```python
ctx.storage.enqueue_notification(
    user_id,
    chat_id,                     # resolve from the user's metadata
    "🔔 текст сообщения",
    kind="reminder",
    dedup_key=f"reminder:{event_id}:{date}",   # makes a periodic scan idempotent
)
```

- `dedup_key` (unique per `(user_id, dedup_key)`) makes re-enqueue a no-op, so a
  periodic worker can run every interval without duplicating messages.
- Delivery is **deny-by-default, twice**: the backend only hands the bridge
  chats on `telegram_effective_allowed_chat_ids`, and the bridge re-checks
  `allowed_chat_ids` before every send (the bot token can reach any chat).
- The bridge drains via signed `GET /api/notifications/pending` +
  `POST /api/notifications/ack` (both gated to `source == "telegram-bridge"`).

## How an Organ is wired in (the integration seam)

In `jericho/organs/__init__.py`, add your organ to `build_registry`:

```python
def build_registry(settings):
    from jericho.organs.reminders import RemindersOrgan
    from jericho.organs.my_organ import MyOrgan
    return OrganRegistry([RemindersOrgan(), MyOrgan()])
```

`create_app` then automatically: registers the organ's capabilities, mounts its
router, and feeds its workers into the supervisor. No other edits are required —
that is the whole point of JOP.

## Reference organs

**`reminders`** (`jericho/organs/reminders/`) — the minimal example: a single
worker. It scans `entity_time` (the timeline) for events inside a lead window,
resolves each user's Telegram chat from their metadata, checks the allowlist,
and enqueues one deduplicated reminder per due event — respecting quiet hours.
Config: `JERICHO_REMINDERS_ENABLED`, `_LEAD_DAYS`, `_POLL_INTERVAL_SEC`.

**`reflection`** (`jericho/organs/reflection/`) — the **full** example: it uses
all three extension points. `capabilities()` contributes `reflection.read`;
`workers()` pushes a weekly self-review digest (deterministic state summary plus
an optional model-synthesised reflection when `ctx.llm` is enabled — the
narrative is a message to the user, never written to the graph); `router()`
serves an on-demand `GET /api/reflection` gated by that capability. Config:
`JERICHO_REFLECTION_ENABLED`, `_INTERVAL_SEC`, `_MIN_KNOWLEDGE`. Copy its
structure when an organ needs a capability + an endpoint, not just a worker.

**`profile`** (`jericho/organs/profile/`) — a pull-only organ: `capabilities()`
+ `router()`, no worker. Computes a user model (recurring people, active
projects, interests) from the graph and serves it at `GET /api/profile`. Copy
it when an organ is a read surface, not a push.

**`chronicle`** (`jericho/organs/chronicle/`) — temporal presence: an "on this
day" resurfacing push (`workers()`) plus an episodic window query (`router()`),
gated by `chronicle.read`. Shows how an organ contributes new **storage
queries** (`list_recent_knowledge`, `list_knowledge_on_this_day`,
`list_entities_by_activity` live in `jericho/storage`) without a schema change.

**`importer`** (`jericho/organs/importer/`) — cold-start bulk intake: a
multipart upload endpoint (`POST /api/import`, cap `import.run`) parsing ICS
calendars, Netscape bookmark exports, mbox mail archives, and single .eml
messages into **pending Inbox items** via `ingest_text(force_review=True)` —
the reference for how bulk material enters the system without ever bypassing
the review gate, and for per-item idempotency via stable `source_ref`s (ics
UID / bookmark URL / Message-ID). Mail is parsed from the original BYTES
(stdlib `mailbox`/`email`, `policy.default`) so declared charsets decode
correctly — follow that pattern for any format whose encoding is
self-described.

**`sentinel`** (`jericho/organs/sentinel/`) — self-monitoring: a single worker
that reuses the read-only `collect_diagnostics` powering the admin panel and
pushes an alert when a real fault appears (a worker crash-looping, the backend
not refreshing state, a missing/invalid backup, an unreachable vLLM). It writes
nothing and derives nothing new — it forwards the diagnostics' own `actions`
(severity `error`/`warning`) as messages, deduplicated per issue per day so a
persistent fault never becomes a stream. The reference for an organ that
**observes existing system state** and reports it, rather than computing over
the knowledge graph. Config: `JERICHO_SENTINEL_ENABLED`, `_INTERVAL_SEC`,
`_CHECK_LLM` (active vLLM port probe).

Аудитория и содержание у sentinel уже́, чем у остальных органов, и это часть контракта:

- **Кому.** Только аккаунты, держащие `admin.diagnostics` — тот же гейт, что и у HTTP-чтения того же отчёта. Иначе исходящий канал становится способом **обойти** модель прав, а не воспользоваться ею. Раньше рассылка шла по всем активным аккаунтам, и `guest`, заведённый одним сообщением в разрешённой группе, получал состояние воркеров, бэкапов и отчёт гигиены секретов чужой машины. Если получателя нет, а неполадка есть, — в лог уходит `WARNING`: молчание не должно быть неотличимо от здоровья.
- **Что.** Из уходящего сообщения вырезается любой абсолютный путь (`‹путь скрыт›` + отсылка к `jericho doctor`). Деталь гигиены секретов — это буквально «‹путь› содержит значение ‹секрет›»; за пределы машины может уехать имя секрета, но не его местоположение. URL-адреса не трогаются, иначе алерт «vLLM недоступен» перестанет называть эндпоинт.
