# Friday: Where MCP Belongs and Where the Native Core Must Remain

> Status: external architecture observation  
> Repository snapshot: `main`, Friday `0.205.0`, 20 August 2026  
> Goal: reduce custom integration work without weakening local-first operation, provenance, privacy, tenant isolation, or the product identity of Friday.

## Executive conclusion

Friday does not need to be redesigned around MCP from scratch. The repository already contains:

- a native MCP runtime in [`friday/mcp_runtime/`](../friday/mcp_runtime/);
- persistent stdio connections with bounded startup and call timeouts;
- code-owned server definitions;
- strict tool allowlists;
- startup checks for required tools;
- structured-result validation;
- a dedicated constrained workspace MCP server;
- the dependency `mcp>=2,<3` in [`pyproject.toml`](../pyproject.toml).

The most important architectural choice is already present in [`friday/mcp_runtime/client.py`](../friday/mcp_runtime/client.py) and [`friday/mcp_runtime/tools.py`](../friday/mcp_runtime/tools.py): remote descriptions, schemas, and annotations are not exposed directly to the model. Friday publishes its own narrow tool contracts and remains responsible for policy, validation, and result projection.

That is the correct foundation. It should be generalized rather than replaced by a universal dynamic MCP client.

## Governing principle

**Do not replace Friday's core with MCP servers. Replace the edges of the system.**

Friday should continue to own:

- memory and meaning;
- provenance and evidence;
- authorization and privacy;
- routing and budgets;
- approvals and idempotency;
- result validation;
- durable user state.

MCP should handle infrastructure that belongs to somebody else:

- OAuth;
- pagination;
- external API churn;
- SaaS connectors;
- browser sessions;
- cloud storage;
- external development and observability systems;
- new external database engines.

A compact formulation:

> Friday owns the brain, memory, and rules. MCP standardizes the external door handles.

## Recommended boundary by subsystem

| Friday subsystem | Recommendation | Practical action |
|---|---|---|
| GitHub, Jira, Linear, Notion, Drive, Slack, mail, calendars, CRM | MCP-first | Do not write full native clients without measured justification |
| Internet search providers | MCP primary plus native fallback | Move provider-specific API work out; retain privacy policy, provenance, and source handling |
| Full browser and JavaScript sites | MCP | Use a browser MCP only when ordinary safe HTTP fetch is insufficient |
| External databases | Hybrid | Keep SQLite, PostgreSQL, and MySQL support; connect new engines through MCP |
| Local workspace | Keep current implementation | The narrow code-owned MCP server is safer than a general filesystem server |
| Telegram bridge | Native | It is a frontend transport, delivery queue, and UX surface, not a simple tool connector |
| Local parsing, OCR, DOCX, XLSX, PDF | Native | Use MCP only for cloud import or publication |
| Whisper, Piper, and local models | Native | They preserve local-first behavior and controlled privacy |
| Memory, retrieval, graph, ingestion, provenance | Native only | These are the product |
| Permissions, approvals, idempotency, tenant isolation | Native only | Never delegate these to an external MCP server |
| Sentry, Grafana, CI, and external diagnostics | MCP-first | Keep Sentinel native, but ingest external signals through adapters |
| LLM provider and runtime layer | Native | Attestation, model profiles, budgets, and fail-closed routing belong here |

## Highest-value current candidate: `web_surfer`

The largest area where Friday currently maintains a great deal of third-party infrastructure is [`friday/web_surfer/`](../friday/web_surfer/).

That contour handles:

- DNS and SSRF protection;
- DNS rebinding protection;
- redirect policy;
- `robots.txt`;
- provider-specific search requests;
- anti-bot responses and provider refusal;
- freshness, language, country, and domain filters;
- global and per-stage deadlines;
- response-body limits;
- HTML parsing;
- PDF extraction;
- complete versus incomplete result classification;
- fallback between search providers.

This work is useful and often carefully implemented, but much of it does not define Friday's unique value. The code even records a concrete provider quirk in which DuckDuckGo returned HTTP 202 placeholder pages that looked like legitimate empty results. These are exactly the kinds of external irregularities that can consume development time forever.

### Keep inside Friday

- the decision whether a specific query may leave the host at all;
- protection against leaking names, internal documents, and personal data;
- prevention of private-entity disclosure to a public search service;
- result-count and body-size limits;
- normalization of returned sources;
- provenance and citations;
- evidence validation;
- classification of complete, partial, or unverifiable answers;
- a simple safe fetch path for public static pages;
- the bounded research workflow.

### Delegate to MCP

- calls to specific search APIs;
- provider authentication;
- pagination;
- provider rate limiting;
- provider-specific retry behavior;
- persistent browser sessions;
- JavaScript rendering;
- authenticated websites;
- clicks, forms, and navigation;
- cookie and session-state management.

### Proposed route

```text
Ordinary search
    -> search MCP
    -> Friday validates and normalizes the result set
    -> native safe fetch for static pages
    -> Friday provenance, evidence, and citations

Complex JavaScript page
    -> browser MCP
    -> Friday receives a bounded structured result
    -> Friday validates source identity, completeness, and limits
    -> the result may enter synthesis
```

The official Playwright MCP server is a reasonable browser-layer candidate. It should not be invoked for every ordinary article. Otherwise the intended simplification becomes a heavy browser process delivering one page together with a large accessibility tree.

## External databases: retain the current native fallback

[`friday/data_sources.py`](../friday/data_sources.py) already models an external database as a source, not as Friday's own storage.

Useful properties of the current implementation include:

- the DSN is not stored in Friday's database;
- only the name of an environment variable is persisted;
- queries are read-only;
- only one `SELECT` or `WITH ... SELECT` statement is allowed;
- SQLite opens in `mode=ro`;
- PostgreSQL receives a read-only connection;
- row limits are enforced;
- timeouts are enforced;
- truncation is reported explicitly;
- schema-description operations exist;
- SQLite, PostgreSQL, and MySQL are supported.

This code is narrow, already implemented, and useful as a fallback. Removing it would not simplify the project.

A sensible new rule is:

> After SQLite, PostgreSQL, and MySQL, do not add new native database drivers to core without strong evidence that MCP is inadequate.

Snowflake, Oracle, ClickHouse, Microsoft SQL Server, BigQuery, MongoDB, and organization-specific engines should normally arrive through MCP.

Friday must still retain its own stable wrapper:

```text
Model
    -> friday_query_external_source
    -> actor / tenant / permission checks
    -> read-only policy
    -> row and time budgets
    -> MCP database server
    -> structured-result validation
    -> provenance and audit
```

The model should not receive a raw `execute_sql` tool from an arbitrary MCP server. Prefer stable code-owned tools such as:

```text
external_source_list
external_source_describe
external_source_query
external_source_sample
```

The model then sees one Friday contract even when the underlying transport changes.

## The current workspace MCP is already close to exemplary

[`friday/mcp_runtime/workspace_fs.py`](../friday/mcp_runtime/workspace_fs.py) should not be replaced with a general filesystem MCP server.

Its surface is intentionally narrow:

- inbox is read-oriented;
- outbox allows only creation of new safe text files;
- no overwrite;
- no append;
- no rename;
- no move;
- no delete;
- no shell;
- no SQLite access;
- no network access;
- paths are validated;
- symlink traversal is blocked;
- selected files are reopened and rechecked;
- descriptor identity, inode, device, size, and timestamps are verified;
- results are bounded;
- extracted sources carry a digest and completeness markers.

Most general filesystem MCP servers expose a much larger surface. Replacing this implementation would trade an existing security boundary for convenience that Friday does not need.

The workspace server should remain the reference design for future connectors:

> A narrow MCP transport outside, with complete policy and repeat validation inside Friday.

## Keep the Telegram bridge native

[`friday/telegram_bridge/`](../friday/telegram_bridge/) does much more than `send_message`:

- receives updates;
- handles commands;
- maintains callback lifecycle;
- builds markup;
- receives and sends media;
- manages queues;
- handles retries;
- connects frontend events to backend operations;
- delivers proactive notifications;
- enforces a deny-by-default chat allowlist;
- participates in idempotency and durable delivery.

MCP is well suited to actions such as:

```text
send_slack_message
create_calendar_event
create_github_issue
upload_to_drive
```

It is poorly suited to replacing a frontend transport that must:

```text
receive Telegram updates
maintain callback lifecycle
render buttons
process commands
operate a durable delivery queue
bind update identity to idempotency fences
```

Therefore:

- Telegram remains a native frontend;
- additional outbound channels may use MCP;
- full inbound Slack, Discord, or Matrix interfaces should be separate transport adapters, not model tools.

## Keep files, OCR, voice, and local models native

Native DOCX, XLSX, PDF, OCR, Whisper, and Piper support Friday's defining properties:

- local-first operation;
- reproducible extraction;
- explicit completeness markers;
- evidence spans;
- controlled privacy;
- review-gated ingestion;
- fail-closed semantics.

Friday has important concepts that ordinary external file tools usually do not provide:

```text
source_complete
verification_eligible
advisory_only
parse_deadline_reached
parse_pages_truncated
archive_truncated
source_truncated_for_parse
```

Delegating the parsing pipeline to an external server may reduce all of this to an unqualified text string and destroy the semantics of trustworthiness.

MCP is useful around native processing:

```text
Drive MCP imports a file
    -> Friday stores original bytes natively
    -> Friday extracts text and evidence natively
    -> ingestion and review remain native

Friday creates a DOCX natively
    -> Drive MCP publishes it

Friday builds a spreadsheet natively
    -> Sheets MCP creates a cloud copy
```

[`friday/whisper.py`](../friday/whisper.py), [`friday/tts.py`](../friday/tts.py), [`friday/ingestion/`](../friday/ingestion/), and [`friday/generated_files.py`](../friday/generated_files.py) should remain inside the product.

## Components that must never be replaced by MCP

The following subsystems are Friday's product DNA:

- `ingestion`;
- `retrieval`;
- `knowledge_graph`;
- `memory`;
- `storage`;
- `permissions`;
- `execution_kernel`;
- `orchestration`;
- `evidence_bundle`;
- `citation_check`;
- `secret_hygiene`;
- `audit_privacy`;
- `source_identity`;
- idempotency fences;
- approvals;
- model attestation;
- the V12 runtime;
- review queues;
- transaction-time and valid-time semantics.

MCP standardizes access to tools, resources, and prompts. It does not provide a ready-made model for:

- personal memory;
- provenance;
- tenant isolation;
- graph identity;
- review workflow;
- temporal semantics;
- evidential answer quality;
- safe repetition of side effects.

MCP can deliver a document to Friday. Friday must decide what that document means, who owns it, and whether it is safe to rely on.

## New SaaS connectors should be MCP-first

A useful default rule for future integrations is:

```text
The external API does not belong to Friday
    -> first look for an official or vendor-maintained MCP server
    -> then write a narrow Friday adapter
    -> create a full native connector only when necessity is measured
```

Recommended trust order:

1. an official server maintained by the service itself;
2. a vendor-maintained server;
3. a well-reviewed open-source server;
4. a narrow server maintained by Friday;
5. a community server only after a dedicated security review.

The MCP Registry is a catalog, not a quality seal or automatic trust boundary. Remote schemas, descriptions, and annotations must be treated as untrusted input.

### Example: GitHub

The official GitHub MCP server already covers repositories, issues, pull requests, Actions, and other GitHub APIs.

The initial integration should be deliberately small:

```text
mode: read-only
toolsets: repositories, issues, pull requests
```

Friday does not need to expose dozens of raw GitHub tools to the model. A handful of stable internal contracts is enough:

```text
github_read_file
github_search_code
github_read_issue
github_list_pull_requests
```

Mutating operations can be introduced later and separately:

```text
github_create_issue
github_comment_issue
github_create_branch
github_open_pull_request
```

They must pass through Friday's approvals, idempotency, and reconciliation logic.

## Fallback must not duplicate the complete implementation

A complete native fallback for every MCP server would erase the expected savings. It would create:

- two implementations;
- two test suites;
- two error surfaces;
- permanent behavior drift.

Fallback should be thin and degraded, not functionally equivalent to the primary implementation.

| Capability | MCP primary | Reasonable fallback |
|---|---|---|
| Web search | Search MCP | Current simple native provider |
| JavaScript browser | Playwright MCP | Try ordinary fetch and explicitly decline the interactive portion |
| GitHub | Official GitHub MCP | Local checkout or explicit unavailability |
| External database | MCP for new engines | Current SQLite, PostgreSQL, and MySQL support |
| Cloud files | Drive or Dropbox MCP | Local inbox and outbox |
| Notifications | Slack or email MCP | Durable queue, not silent delivery through a different channel |
| Calendar | Calendar MCP | Offer a local reminder only after explicit user consent |

### Safe automatic fallback cases

Only for safe read operations and only with a clear failure category:

- server unavailable;
- startup timeout;
- transport failure before execution begins;
- missing allowlisted tool;
- protocol violation followed by disabling the suspicious server;
- temporary failure of a read-only operation.

### Cases where fallback must be forbidden

- `PermissionError`;
- policy denial;
- invalid arguments;
- user refusal;
- a domain error such as "issue not found";
- unknown outcome of a mutating operation;
- any situation in which an external effect may already have occurred.

A particularly dangerous case is:

```text
Calendar MCP receives create_event
    -> the connection drops
    -> Friday invokes a native fallback
    -> two events are created
```

The correct response is:

```text
status = uncertain
    -> check the postcondition
    -> determine whether the effect occurred
    -> only then decide whether another attempt is safe
```

This philosophy already exists in Friday's side-effect, mission, and idempotency logic. It should be applied consistently to MCP integrations.

## Split MCP error classes

At present, [`MCPUnavailableError`](../friday/mcp_runtime/client.py) covers several materially different cases:

- server unavailable;
- missing tool;
- tool returned `is_error`;
- invalid structured result;
- transport failure;
- retired connection;
- timeout.

That is acceptable for manual use, but insufficient for safe automatic fallback.

A more useful taxonomy would be:

```python
class MCPError(RuntimeError):
    pass


class MCPTransportUnavailable(MCPError):
    """A trusted read-only fallback may be permitted."""


class MCPProtocolViolation(MCPError):
    """Disable the server; policy may permit a read-only fallback."""


class MCPRemoteRejected(MCPError):
    """The remote tool processed and rejected the request. Do not retry automatically."""


class MCPPolicyDenied(MCPError):
    """Friday policy denied the operation. Fallback is forbidden."""


class MCPUncertainEffect(MCPError):
    """The effect may have happened. Reconciliation and a postcondition are required."""
```

## Make capability routing declarative

The model should not need to know which transport implements a capability.

A useful abstraction would be:

```python
@dataclass(frozen=True)
class CapabilityRoute:
    name: str
    primary: ToolProvider
    fallback: ToolProvider | None
    risk: Literal["observe", "mutate", "high"]
    fallback_on: frozenset[type[MCPError]]
    postcondition: Callable[..., Awaitable[bool]] | None = None
```

The overall architecture remains:

```text
Model
    -> Friday ToolSpec
    -> ExecutionKernel
    -> permissions / budgets / approvals
    -> code-owned capability adapter
        -> MCP primary
        -> native degraded fallback
    -> result validation
    -> provenance / audit
```

The model should not care whether a Friday ToolSpec is implemented by:

- an MCP server;
- a Python function;
- a local subprocess;
- an HTTP adapter;
- a queued human approval.

The contract belongs to Friday.

## Friday should also become an MCP server

This is a separate strategically valuable move.

Friday currently builds its own frontends and agent interfaces. If Friday also exposes a local MCP server, other agent environments can use its memory without requiring a dedicated integration for each one.

### Candidate resources

```text
friday://documents/{document_id}
friday://entities/{entity_id}
friday://conversations/{conversation_id}
friday://timeline/{local_date}
friday://evidence/{bundle_id}
```

### Read-only tools

```text
friday_search
friday_find_person
friday_graph_neighbors
friday_get_timeline
friday_explain_evidence
```

### Mutating tools

```text
friday_remember
friday_ingest
friday_create_reminder
friday_resolve_conflict
```

Resources are appropriate for addressable data and context. Tools are appropriate for computations and actions.

The first version should be deliberately narrow:

- local stdio;
- owner-only;
- read-only;
- fixed allowlist;
- no remote dynamic discovery;
- the same tenant, permission, and evidence boundaries;
- no mutating tools.

A safe initial set is:

```text
friday_search
friday_explain_evidence
friday://documents/{id}
friday://entities/{id}
```

After stabilization, `remember` and reminder operations can be added behind approvals.

This would make Friday not only an assistant, but also a personal memory backend for other agents.

## Suggested implementation sequence

### Phase 1: generalize the current MCP runtime

Without changing current behavior:

- split the error classes;
- add `CapabilityRoute`;
- document fallback policy;
- add health state;
- add a circuit breaker;
- add metrics for latency, failure class, and unavailable duration;
- preserve fixed wrappers;
- preserve the prohibition on publishing remote schemas to the model;
- preserve code-owned server definitions.

### Phase 2: connect the first official external MCP

A suitable candidate is the official GitHub MCP server in read-only mode.

Initial constraints:

- repositories, issues, and pull requests only;
- a small set of Friday-owned ToolSpecs;
- tenant-scoped credentials;
- pinned server version or image digest;
- bounded result projection;
- no write tools;
- complete call auditing;
- native fallback only to a local checkout.

This validates the general architecture against a mature server without risking memory or private documents.

### Phase 3: split the web path

- retain static safe fetch natively;
- allow search providers through MCP;
- use browser MCP as an escalation path;
- initially forbid browser write actions;
- pass results through existing provenance and citation machinery;
- retain the privacy classifier before any external query;
- retain domain and freshness policy inside Friday.

### Phase 4: expose a Friday MCP server

Initial version:

```text
friday_search
friday_explain_evidence
friday://documents/{id}
friday://entities/{id}
```

Mode:

- local;
- owner-only;
- read-only;
- bounded;
- audited.

### Afterward

- connect new SaaS products through MCP;
- connect new database engines through MCP;
- connect new cloud storage through MCP;
- avoid new native OAuth clients without strong justification;
- introduce every mutating connector separately after read-only operation is proven;
- treat every external MCP server as an untrusted process.

## Work that should leave Friday core

- full SaaS API clients;
- per-service OAuth implementations;
- provider-specific pagination;
- a full custom browser orchestrator;
- support for additional database engines;
- dedicated GitHub, Jira, and Linear clients;
- cloud publication plumbing;
- low-level clients for external observability platforms.

## Work that should remain native

- ingestion;
- memory;
- retrieval;
- graph;
- provenance;
- evidence;
- permissions;
- privacy;
- approvals;
- idempotency;
- temporal semantics;
- local file storage;
- Telegram transport;
- local voice;
- model runtime;
- verifier;
- review workflow;
- audit.

## Final assessment of project scope

Friday is no longer a simple Telegram bot. The current repository is a local knowledge platform with:

- an ingestion pipeline;
- immutable raw objects;
- a knowledge graph;
- hybrid retrieval;
- temporal graph semantics;
- an agent runtime;
- an execution kernel;
- missions;
- permissions;
- a privacy plane;
- an admin UI;
- a Telegram frontend;
- backup and restore;
- diagnostics;
- voice;
- file and OCR handling;
- an MCP runtime;
- operational supervision.

It is objectively expensive to build a product of this scope while also maintaining a custom implementation of every external API.

The product vision does not need to shrink into an ordinary bot. The territory Friday must maintain directly should shrink.

**Final formula:** keep the brain, memory, evidence, and rules native. Standardize external integrations through MCP.

## Repository files reviewed

- [`README.md`](../README.md)
- [`pyproject.toml`](../pyproject.toml)
- [`.env.example`](../.env.example)
- [`friday/mcp_runtime/client.py`](../friday/mcp_runtime/client.py)
- [`friday/mcp_runtime/tools.py`](../friday/mcp_runtime/tools.py)
- [`friday/mcp_runtime/workspace_fs.py`](../friday/mcp_runtime/workspace_fs.py)
- [`friday/web_surfer/__init__.py`](../friday/web_surfer/__init__.py)
- [`friday/data_sources.py`](../friday/data_sources.py)
- [`friday/telegram_bridge/`](../friday/telegram_bridge/)
- [`friday/ingestion/`](../friday/ingestion/)
- [`friday/generated_files.py`](../friday/generated_files.py)
- [`friday/whisper.py`](../friday/whisper.py)
- [`friday/tts.py`](../friday/tts.py)
- [`friday/execution_kernel/`](../friday/execution_kernel/)
- [`docs/ORGANS.md`](../docs/ORGANS.md)
- [`docs/EXECUTIVE.md`](../docs/EXECUTIVE.md)

## External reference points

- MCP Specification: <https://modelcontextprotocol.io/specification/>
- MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk>
- MCP Registry: <https://github.com/modelcontextprotocol/registry>
- GitHub MCP Server: <https://github.com/github/github-mcp-server>
- Playwright MCP: <https://github.com/microsoft/playwright-mcp>
