# Friday Sensitive Document Handling and Secure Workbench Architecture

> Status: external security architecture observation  
> Repository snapshot: `main`, Friday `0.205.0`, 20 August 2026  
> Scope: sensitive document ingestion, channel policy, derivative data labels, local processing, Telegram boundaries, Obsidian evaluation, and the minimum native Secure Friday Workbench.  
> Related observations: [`MCP_ARCHITECTURE_OBSERVATION.md`](MCP_ARCHITECTURE_OBSERVATION.md) and [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md).

## Executive conclusion

Friday should treat the requirement "sensitive documents must not leave the controlled environment" as an architectural boundary, not as a prompt instruction or a convenience setting.

For material in the restricted class:

- the document body must not be sent through Telegram;
- generated answers, summaries, filenames, entities, tags, excerpts, thumbnails, embeddings, and other derivatives must not be published through Telegram or any external service;
- public web search and external MCP calls must be denied before a restricted payload is serialized;
- parsing, OCR, embedding, reranking, synthesis, storage, and review must run inside a Secure Core with no general internet egress;
- the user needs a separate local interface for search, reading, annotations, approvals, and exports;
- Obsidian may remain useful for prototypes and explicitly permitted non-restricted projections, but it should not be the production plaintext viewer for restricted Friday material.

The recommended product is not a second general-purpose Obsidian clone. It is a smaller, policy-owned **Secure Friday Workbench**:

```text
search
catalog and saved views
document reader
evidence and provenance
relations and backlinks
annotations
tabs, splits, and command palette
```

The most important missing capability is not another notebook. It is a complete sensitive-data handling plane that follows every source and every derivative across storage, models, tools, channels, logs, caches, backups, and exports.

## Decision statement

Adopt the following direction:

```text
Secure Core with default-deny egress
    +
typed security labels and derivative lineage
    +
guarded Network Edge
    +
native Secure Friday Workbench
```

Do not use Telegram as an intake or publication channel for restricted content.

Do not make Obsidian the canonical store or required production viewer for restricted content.

Use the existing Obsidian-like prototype only as a source of interface components and interaction ideas, then connect those components to Friday's native catalog, retrieval, evidence, graph, annotation, and permission contracts.

## Requirement levels must be distinguished

Two policies are often described informally as "local only", but they are materially different.

### Policy A: plaintext may be processed by approved software on the controlled host

Under this policy, a third-party desktop viewer can be acceptable if:

- the process has no network egress;
- the local storage is encrypted at rest;
- no unapproved extensions or plugins execute;
- updates are controlled;
- the application receives only the permitted projection;
- the application is accepted as part of the trusted computing base.

A hardened Obsidian installation can potentially fit this policy, although it still expands the trusted computing base.

### Policy B: no third-party application may receive plaintext

Under this stricter policy, Obsidian is not acceptable regardless of whether Sync is disabled or the process is firewalled. The application itself would still receive the plaintext.

The Secure Friday Workbench is required for Policy B.

The organization must choose and record which policy applies. Friday should not silently assume that "no cloud sync" is equivalent to "no third party".

## Trust-boundary model

The system should be divided into two operational zones.

```text
┌──────────────────────────────────────────────┐
│ Secure Core                                  │
│                                              │
│ Raw objects and inbox                        │
│ Document catalog and knowledge               │
│ Conversation storage                         │
│ OCR and document parsers                     │
│ Local embeddings and reranker                │
│ Local LLM runtime                            │
│ Permissions, approvals, provenance           │
│ Security labels and derivative lineage       │
│ Secure Friday Workbench                      │
│                                              │
│ Default: no general internet egress          │
└──────────────────────┬───────────────────────┘
                       │
                       │ typed guarded broker
                       │ minimal approved payloads only
                       │
┌──────────────────────▼───────────────────────┐
│ Network Edge                                 │
│                                              │
│ Telegram transport                           │
│ Public web search                            │
│ External MCP servers                         │
│ SaaS and cloud connectors                    │
│ Update and notification adapters             │
│                                              │
│ No document-store, database, or model mounts │
└──────────────────────────────────────────────┘
```

The boundary must be enforced by operating-system and container controls in addition to application code.

A Python condition such as:

```python
if document.security_level == "restricted":
    deny()
```

is useful but insufficient by itself. A future call site, log statement, helper, plugin, or serialization path can bypass it accidentally.

The stronger design is:

- Secure Core cannot establish arbitrary outbound connections;
- Network Edge cannot mount or read the document store;
- all crossings use a small typed protocol;
- policy is evaluated before payload construction and serialization;
- the broker records metadata about the decision without recording sensitive bodies;
- failure is closed, explicit, and observable.

## Telegram is not a restricted-content channel

Telegram distinguishes cloud chats from Secret Chats. Its official FAQ describes Secret Chats as end-to-end encrypted, while cloud chat data is part of Telegram's distributed infrastructure. Telegram's bot documentation states that bots are connected through Telegram's intermediary server and that the developer communicates with that server through the Bot API.

A bot conversation is therefore not equivalent to a device-to-device Secret Chat. Running the Bot API server locally changes the backend connection arrangement, but does not turn the user's bot conversation into an end-to-end encrypted restricted channel.

For Friday, the practical rule should be:

```text
Telegram:
    ordinary requests and permitted material only
    content-free restricted notifications allowed
    restricted intake denied
    restricted answer publication denied
    restricted filenames, entities, excerpts, and metadata denied
```

An acceptable notification is:

```text
Processing completed. Open the local secure workbench.
```

An unacceptable notification is:

```text
Analysis of Project Alpha staffing order 14/7 is complete.
```

The second message can leak a project, document type, number, and activity even without the body.

### Telegram policy must cover both directions

Inbound:

- reject or quarantine a restricted upload before normal ingestion;
- do not rely on the user to remember the rule;
- show a clear message directing the user to local secure intake;
- do not extract, OCR, summarize, or classify the file through the Telegram path.

Outbound:

- do not send restricted bodies or derivatives;
- do not send restricted generated files;
- do not send citations that reveal restricted titles or identifiers;
- do not include sensitive data in error messages;
- do not place restricted content in retry queues or dead-letter payloads;
- generic completion and failure notifications may be allowed.

## Sensitive data is larger than the original file

Protecting only the raw PDF, DOCX, spreadsheet, archive, or image is not enough.

A single source can produce:

```text
raw file
  -> extracted text
  -> OCR pages
  -> structural representation
  -> document passages
  -> entities and relations
  -> typed dates
  -> title and summary
  -> tags and facets
  -> embeddings
  -> thumbnails and previews
  -> search excerpts
  -> model prompts and outputs
  -> generated reports
  -> audit and diagnostic events
```

Every item can reveal the source directly or indirectly.

The system should therefore use explicit security labels and lineage for all derived objects.

## Proposed security-label contract

The exact level names must be mapped to the organization's policy. An illustrative contract is:

```python
SecurityLabel(
    level="restricted",
    compartments=["project-alpha", "legal"],
    tenant_id="...",
    owner_scope="...",
    allowed_channels=["local_secure_workbench"],
    allowed_processors=[
        "local_parser",
        "local_ocr",
        "local_embedding",
        "local_reranker",
        "local_llm",
    ],
    external_web=False,
    external_mcp=False,
    telegram=False,
    remote_model=False,
    export_requires_approval=True,
    retention_policy="case-policy-7",
    source_label_ids=["..."],
)
```

The label must be attached to:

- raw objects;
- inbox items;
- knowledge objects;
- chunks and passages;
- embeddings and rerank caches;
- entities and relations when their existence is sensitive;
- conversations and messages;
- generated files;
- notifications;
- annotations;
- saved views when their query or title reveals sensitive information;
- temporary files;
- backups and restore manifests.

### Derivative propagation rule

The minimum safe rule is:

```text
label(derivative) >= join(labels(all source evidence))
```

Examples:

```text
restricted document -> restricted summary
restricted document -> restricted embedding
restricted document -> restricted thumbnail
restricted document -> restricted search excerpt
restricted + confidential sources -> restricted synthesis
```

No model may downgrade a label.

No output may be considered public merely because it is short, paraphrased, aggregated, or vectorized.

### Aggregation can increase sensitivity

Combining individually lower-class facts can produce a more sensitive result.

For example:

```text
staff names
+ dates
+ locations
+ project relationships
= operational picture
```

The label engine should support explicit aggregation uplift, either by deterministic policy or by a human classification workflow. The default must never be automatic downgrade.

## Derivative lineage and lifecycle

Every material derivative should record:

```text
object_id
source_object_ids
transformation_kind
processor_identity
processor_version
created_at
security_label_id
content_digest
completeness
superseded_by
deleted_or_invalidated_at
```

This enables Friday to answer:

- Which summaries came from this document?
- Which embeddings must be rebuilt after reclassification?
- Which reports contain evidence from a deleted source?
- Which generated files must be blocked after access revocation?
- Which cached excerpts are stale?
- Which graph edges exist only because of restricted evidence?

When a source is deleted, reclassified, corrected, or access-revoked, Friday must:

- delete or invalidate its derivatives;
- rebuild affected indexes;
- remove or tombstone affected graph material;
- invalidate generated reports;
- revoke publication capability;
- preserve only the minimum policy-compliant audit trail.

Deletion without derivative closure is not complete deletion.

## Policy must be checked before serialization

The strongest publication gate sits before data becomes an outbound payload.

Bad sequence:

```text
build complete Telegram message
  -> log it
  -> enqueue it
  -> policy says no
```

Safe sequence:

```text
identify destination channel
  -> compute effective label
  -> authorize channel and action
  -> construct a bounded permitted projection
  -> serialize
  -> send
```

The same principle applies to:

- web search queries;
- MCP tool calls;
- remote model requests;
- email;
- generated file download;
- printing;
- clipboard copy;
- export;
- backup;
- observability.

A denied action should never leave the denied body in a queue, trace, exception, or diagnostic bundle.

## External tools and restricted evidence

For any operation whose evidence set includes restricted material:

```text
external web search: denied
external MCP: denied
remote LLM: denied
cloud OCR: denied
cloud transcription: denied
external document conversion: denied
remote telemetry with bodies: denied
```

This is a taint rule, not a model suggestion.

The model may request a tool, but the runtime policy layer decides whether the tool is available for the current evidence and destination.

### Public research beside restricted work

A user may ask a public-world question while a restricted document is open. Friday must not automatically mix the restricted context into the public query.

A safe planner separates the tasks:

```text
public subquestion
    -> minimal public query
    -> Network Edge
    -> bounded public results
    -> Secure Core

restricted subquestion
    -> local retrieval and local models only
```

The public query must be constructed from explicitly public inputs, not from the full conversation or active document context.

## Obsidian evaluation

Obsidian is useful, mature, and highly productive, but its security properties must be evaluated against the actual requirement.

Official Obsidian documentation states that:

- end-to-end encryption applies to remote vaults used by Obsidian Sync;
- Obsidian does not encrypt the local vault;
- community plugins execute third-party code;
- Obsidian cannot reliably restrict plugins to specific permissions;
- community plugins can access files, connect to the internet, and install additional programs.

Therefore disabling Sync alone is not a sufficient restricted-data boundary.

### When Obsidian may still be acceptable

Under Policy A, a managed installation may be considered with all of the following:

```text
separate operating-system account
encrypted storage volume
network egress blocked for the process
Restricted Mode enabled
community plugins disabled
no remote images or embeds
no Sync or Publish
controlled offline updates
read-only managed projection
writable annotations stored separately
no access to Friday's database, secrets, or raw storage
```

Even then, Obsidian becomes part of the trusted computing base and must be accepted explicitly.

### When Obsidian is not acceptable

Under Policy B, or whenever the organization does not approve Obsidian as a plaintext processor, it must not receive restricted material.

The correct response is not to create a more elaborate vault integration. It is to provide the required native workflow inside Friday.

## Secure Friday Workbench

The Workbench should be a local, first-party interface backed by Friday's own typed APIs and permission system.

It should not use a Markdown folder as the source of truth.

It should not expose physical storage paths.

It should not duplicate Friday's catalog, graph, provenance, or security model.

### 1. Universal archive search

One search box should accept:

```text
approximate content
approximate date
person
organization
document type
reference number
project
conversation history
tag or facet
```

The Workbench should call the unified archive-search contract recommended in [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md).

Results should expose:

- authority state;
- security level;
- source kind;
- date roles;
- matched passages;
- provenance;
- completeness;
- access reason;
- whether the source is pending, canonical, superseded, or partial.

### 2. Catalog and saved views

Useful first-party views include:

```text
documents received this week
documents dated March 2024
pending review
restricted documents
partial extraction
missing document date
documents related to Person X
documents related to Project Y
recently changed classifications
generated reports awaiting export approval
```

Saved views should be typed query objects, not only Markdown files with embedded query syntax.

### 3. Document reader

The reader should present:

```text
original source
safe rendered view
extracted text
document structure
summary
typed dates
tags and facets
entities and relations
evidence passages
coverage warnings
provenance
versions
security label
derivative inventory
```

Opening the original must occur through an authorized local endpoint or file broker. The browser should not receive a permanent physical path.

### 4. Backlinks and related material

The related-material panel should show:

```text
linked people
linked organizations
related documents
source conversations
annotations
generated reports
conflicts
versions
derived objects
```

Friday's graph and provenance layers should remain authoritative. Markdown `[[wikilinks]]` may be rendered as a convenience but should not define identity or authorization.

### 5. Annotations

Annotations should be first-class records:

```text
Annotation
    id
    tenant_id
    author_id
    body_markdown
    linked_document_ids
    linked_entity_ids
    security_label_id
    created_at
    updated_at
    version
```

The label should normally inherit from the linked material unless a stricter rule applies.

Annotations must be separable from the immutable source and reviewable independently.

### 6. Tabs, splits, and command palette

These features create most of the productive desktop-workspace experience without requiring a full Obsidian clone:

- multiple open documents;
- document and annotation side by side;
- saved layouts;
- keyboard-first navigation;
- command palette;
- recent items;
- pinned searches;
- approval panels.

## Explicit Workbench non-goals

The first secure release should not implement:

- a community plugin ecosystem;
- arbitrary JavaScript execution;
- arbitrary HTML;
- cloud sync;
- public publishing;
- a theme marketplace;
- Canvas;
- a universal graph visualization;
- generic filesystem editing;
- Markdown files as the canonical database;
- automatic remote embeds;
- unrestricted iframe content;
- full bidirectional vault synchronization;
- cross-device sync before a separate security design exists.

The graph view is visually attractive, but search, reader, evidence, backlinks, annotations, and saved views provide more immediate value and a much smaller attack surface.

## Reuse of the existing local prototype

The existing Obsidian-like prototype is not wasted work.

Reusable components likely include:

- application shell;
- sidebar;
- tabs;
- split panes;
- document tree;
- Markdown renderer;
- annotation editor;
- command palette;
- relation or backlink panel;
- layout persistence.

Components to remove or freeze:

- generic filesystem watcher;
- bidirectional folder mirroring;
- plugin API;
- generic sync;
- file identity based only on path or title;
- complex Markdown conflict resolution;
- arbitrary extension loading;
- remote content loading.

The adapted interface should connect to:

```text
DocumentCatalog
ArchiveSearch
EvidenceBundle
EntityGraph
Annotations
SavedViews
WorkbenchLayout
ApprovalService
SecurityPolicy
DerivativeLineage
```

The Workbench is a controlled projection of Friday, not a second knowledge system beside Friday.

## Local frontend security

`localhost` is not a security boundary by itself.

The Workbench should use:

- bundled local JavaScript, CSS, icons, and fonts;
- a strict Content Security Policy;
- `connect-src` limited to the local authorized endpoint;
- `img-src` limited to local resources and explicitly approved data URLs;
- no external frames;
- no remote scripts;
- no automatic remote image loading;
- sanitized Markdown and HTML;
- `Cache-Control: no-store` for sensitive responses;
- no document bodies in `localStorage`;
- no document bodies in IndexedDB;
- no service-worker cache for sensitive content;
- no analytics or external crash reporting with bodies;
- a dedicated browser profile without extensions;
- short-lived local authorization;
- explicit session lock and idle timeout;
- audited download, print, clipboard, and export actions.

Browser extensions are especially important. An ordinary browser profile can contain extensions that read page content. A dedicated extension-free profile or managed application shell is safer.

## Local processing requirements

Restricted processing should remain local:

```text
document parsing
archive extraction
OCR
image rendering
speech transcription
embeddings
reranking
LLM synthesis
report generation
```

Additional rules:

- documents are data, never instructions;
- macros and embedded executable content are never run;
- parsers should be sandboxed where practical;
- remote references inside documents are not fetched automatically;
- external links do not receive a referrer containing local context;
- model prompts and outputs are not logged;
- temporary files use protected storage and restrictive permissions;
- generated previews inherit the source label;
- failures do not fall back to cloud processors.

A silent cloud fallback is a security incident.

## Host and storage hardening

A secure deployment should address:

- full-disk or volume encryption;
- restrictive file ownership and permissions;
- separate service and interactive identities;
- encrypted or disabled swap;
- disabled or protected core dumps;
- protected temporary directories;
- encrypted backups;
- restore testing inside the same policy boundary;
- no plaintext document bodies in shell history;
- no plaintext document bodies in process arguments;
- no unrestricted diagnostic bundles;
- controlled update artifacts;
- trusted local model and parser artifacts;
- integrity verification for deployed components.

Encryption at rest does not replace access control, and access control does not replace egress control. All three are required.

## Channel policy matrix

An initial policy can be expressed as follows.

| Channel or sink | Public | Internal | Confidential | Restricted |
|---|---:|---:|---:|---:|
| Secure Friday Workbench | allow | allow | allow | allow |
| Local attested parser or model | allow | allow | allow | allow |
| Telegram body or file | allow by policy | configurable | deny by default | deny |
| Telegram content-free notice | allow | allow | allow | allow |
| Public web search | allow | allow only from public projection | deny by default | deny |
| External MCP | allow by tool policy | configurable | deny by default | deny |
| Remote LLM or OCR | configurable | deny by default | deny | deny |
| Generated local file | allow | allow | allow with inherited label | allow with inherited label |
| External export | allow | approval by policy | explicit approval | explicit declassification or exceptional approval |
| Logs and metrics | shape only | shape only | shape only | shape only |
| Backup | protected | protected | encrypted and controlled | encrypted and controlled |
| Clipboard, print, download | allow | audited by policy | audited | audited and approval-controlled |

The exact levels and exceptions must be configured from organizational policy. The important property is that the matrix is centralized, testable, and evaluated for every effect.

## Approval is not declassification

A human approval to perform an action does not automatically change the security label.

Examples:

```text
approve local report generation
    != approve Telegram publication

approve download to protected folder
    != approve upload to cloud storage

approve printing
    != classify the content as public
```

Exceptional export should require:

- destination;
- purpose;
- actor;
- source evidence;
- effective label;
- expiry;
- exact output digest;
- fresh approval;
- audit event without body leakage.

Where declassification is permitted, it must be a separate explicit operation.

## Threat model

The minimum threat model includes:

### External-channel exfiltration

Sensitive content reaches Telegram, web search, external MCP, remote models, email, or telemetry.

Mitigation:

- physical egress separation;
- pre-serialization channel policy;
- no document mounts in Network Edge;
- deny-by-default tool availability.

### Derivative leakage

The raw file is protected but a summary, embedding, filename, thumbnail, graph relation, or generated report escapes.

Mitigation:

- label propagation;
- derivative lineage;
- security-aware caches and indexes;
- destination checks on every effect.

### Local cache and log leakage

Content remains in browser cache, temporary files, model logs, parser traces, crash reports, or queue payloads.

Mitigation:

- no-store responses;
- protected temporary storage;
- body-free logs;
- body-free diagnostics;
- queue payload minimization;
- secure deletion or invalidation policy.

### Untrusted document behavior

A document contains prompt injection, malicious links, active content, macros, remote images, or parser exploits.

Mitigation:

- treat content as data;
- sandbox parsers;
- never execute macros;
- block automatic network fetches;
- sanitize rendering;
- use local content limits and time budgets.

### Third-party viewer or extension access

Obsidian plugins, browser extensions, or other desktop integrations read plaintext.

Mitigation:

- first-party Workbench;
- dedicated browser profile or application shell;
- no community plugins;
- explicit trusted-computing-base decision.

### Unauthorized local access

Another account, process, backup operator, or administrator reads sensitive material.

Mitigation:

- OS identity separation;
- encrypted storage;
- restrictive permissions;
- audited privileged access;
- locked sessions.

### Incorrect lifecycle closure

A source is deleted or reclassified, but derivatives remain searchable or exportable.

Mitigation:

- lineage traversal;
- cache invalidation;
- index rebuild;
- report revocation;
- acceptance tests for closure.

## Implementation sequence

### Phase 0: policy decision

Record:

- classification taxonomy;
- whether Policy A or Policy B applies;
- approved local processors;
- allowed channels by level;
- export and declassification rules;
- retention and backup policy;
- operator and tenant boundaries.

No sensitive production material should enter Friday until the minimum restricted path is defined.

### Phase 1: labels and lineage

Implement:

- typed security labels;
- label inheritance;
- source-to-derivative lineage;
- effective-label joins;
- aggregation uplift hooks;
- reclassification and deletion closure;
- label-aware generated files and annotations.

### Phase 2: outbound effect broker

Centralize:

- Telegram publication;
- web queries;
- MCP calls;
- remote model calls;
- exports;
- notifications;
- downloads and generated-file delivery.

Authorize before serialization.

### Phase 3: physical Secure Core and Network Edge split

Enforce:

- no general egress from Secure Core;
- no document or database mounts in Network Edge;
- typed broker protocol;
- minimal payloads;
- fail-closed behavior;
- deployment-level tests.

### Phase 4: secure local intake and Workbench

Deliver:

- local upload;
- universal search;
- catalog;
- document reader;
- evidence and provenance;
- annotations;
- approvals;
- content-free Telegram notifications.

### Phase 5: frontend and operational hardening

Add:

- strict CSP;
- extension-free browser profile or managed shell;
- no-store caching;
- idle lock;
- secure temporary storage;
- export controls;
- encrypted backup and restore tests;
- deployment integrity checks.

### Phase 6: optional non-restricted Obsidian projection

Only after the native path is stable:

- project explicitly allowed content;
- use read-only generated Markdown where useful;
- keep canonical identity and permissions in Friday;
- never use this path as a fallback for restricted material.

## Acceptance criteria

The restricted path is not ready until all of the following are demonstrated.

### Network and channel

- Processing a restricted document produces no outbound packets from Secure Core.
- A restricted request cannot invoke external web, MCP, OCR, or model services.
- Denial occurs before serialization.
- Telegram receives only an approved content-free notification.
- Network Edge has no path to the raw store, database, index, or model prompt cache.

### Labels and derivatives

- Every stored derivative carries an effective security label.
- Multi-source synthesis uses the join of all source labels.
- Embeddings, thumbnails, excerpts, entities, relations, and generated files are covered.
- Reclassification invalidates or relabels all dependent objects.
- Deletion closes searchable and exportable derivatives.

### Storage and observability

- Logs, metrics, traces, and audits contain no restricted bodies.
- Temporary files and previews use protected storage.
- Browser storage contains no document body.
- Backups are encrypted and restore into the same policy boundary.
- Diagnostic bundles are body-free by construction.

### User workflow

- The user can ingest, locate, read, annotate, and review restricted documents without Telegram.
- Search results explain authority, provenance, completeness, and security status.
- The Workbench supports tabs, splits, saved views, and approval flows.
- Export is explicit, destination-bound, digest-bound, and audited.

## Suggested executable regression tests

Friday's test suite already favors human-readable invariant names. Candidate additions include:

```text
test_a_restricted_document_never_crosses_the_network_broker.py
test_a_restricted_answer_never_reaches_telegram.py
test_a_content_free_notice_contains_no_document_identity.py
test_a_derivative_inherits_the_strictest_source_label.py
test_an_embedding_is_not_treated_as_public_metadata.py
test_a_reclassification_invalidates_every_dependent_projection.py
test_a_deleted_source_leaves_no_exportable_report.py
test_the_network_edge_has_no_document_store.py
test_the_secure_workbench_loads_no_remote_resource.py
test_the_browser_cache_keeps_no_restricted_body.py
test_an_export_is_bound_to_one_digest_and_destination.py
test_an_approval_does_not_declassify_the_material.py
test_a_cloud_fallback_fails_closed.py
```

Deployment tests should also inspect:

- effective container mounts;
- network namespaces;
- firewall rules;
- DNS access;
- process environment;
- open ports;
- backup destinations;
- browser profile configuration.

## What this changes in the product roadmap

The secure Workbench is not a decorative replacement for Telegram. It becomes the required primary interface for high-sensitivity work.

The roadmap should therefore prioritize:

1. data labels and lineage;
2. centralized outbound effects;
3. physical egress separation;
4. secure local intake;
5. search and reader;
6. annotations and approvals;
7. desktop workflow polish.

A full Obsidian-style plugin platform, Canvas, theme ecosystem, and generic filesystem model should remain outside the restricted-work roadmap.

## Final recommendation

Friday should keep Telegram as an excellent operational interface for permitted material, reminders, ordinary conversation, and content-free status notifications.

Restricted documents require a different route:

```text
local secure intake
    -> Secure Core
    -> local parsing, retrieval, and models
    -> Secure Friday Workbench
    -> explicit local review
    -> controlled export when policy permits
```

The existing Obsidian-like work should be narrowed rather than discarded. Reuse its shell, navigation, tabs, splits, editor, and command palette. Remove the generic vault ambitions and attach the interface directly to Friday's own document catalog, archive search, evidence, provenance, graph, annotations, approvals, and security labels.

The target is not "another Obsidian".

The target is a local document cockpit whose walls are part of Friday's security model.

## External references

Official sources checked on 20 August 2026:

- [Telegram FAQ](https://telegram.org/faq)
- [Telegram: Bots, an introduction for developers](https://core.telegram.org/bots)
- [Telegram: Working with bots](https://core.telegram.org/api/bots)
- [Obsidian Sync security and privacy](https://help.obsidian.md/Obsidian%20Sync/Security%20and%20privacy)
- [Obsidian plugin security](https://obsidian.md/help/plugin-security)
- [Obsidian community plugins](https://obsidian.md/help/community-plugins)
