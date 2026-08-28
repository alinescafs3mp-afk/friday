# Friday System Assurance and Records Governance Architecture

> Document ID: FRIDAY-ARG-001  
> Status: future architecture reference; no implementation authority; live
> state and owner decisions are owned by [`PROJECT_BACKLOG.md`](PROJECT_BACKLOG.md)
> Repository snapshot: `main`, Friday `0.205.0`, 20 August 2026  
> Scope: information-flow governance, records authority, classification lifecycle, deployment assurance, key management, parser isolation, inference security, incident response, controlled export, archival portability, and architecture fitness.  
> Related documents: [`PROJECT_BACKLOG.md`](PROJECT_BACKLOG.md), [`DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md`](DOCUMENT_AND_MESSAGE_RETRIEVAL_AUDIT.md), and [`SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md`](SENSITIVE_DOCUMENT_HANDLING_AND_SECURE_WORKBENCH.md).

## Executive conclusion

Friday already has many of the organs expected from a serious local-first knowledge system:

- immutable source capture;
- provenance-aware ingestion;
- document parsing and OCR;
- review queues;
- knowledge objects and a graph;
- hybrid retrieval;
- permissions and tenant boundaries;
- execution controls and approvals;
- generated files;
- backup and restore;
- operational diagnostics;
- Telegram and local interfaces;
- MCP transport;
- V12 orchestration and model attestation work.

The remaining architectural gap is not another end-user feature. It is a system-wide **assurance and records-governance plane** that answers three questions consistently:

```text
1. Where may each source and derivative flow?
2. What is Friday entitled to claim about a record?
3. What executable evidence proves that the rules are enforced?
```

Without this plane, the system can have strong individual controls while still permitting unsafe combinations, ambiguous record authority, incomplete deletion, unprovable deployment claims, or accidental plaintext projections.

The recommended direction is:

```text
Information-flow governance
    +
Record authority and lifecycle
    +
System assurance
```

These three layers should become the roof over the previously proposed document catalog, unified retrieval, Secure Core, Network Edge, Secure Friday Workbench, MCP adapters, and V12 runtime.

## Normative language

The terms **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** describe architectural requirements.

- **MUST** and **MUST NOT** identify release-blocking invariants.
- **SHOULD** and **SHOULD NOT** identify strong defaults that require an explicit recorded exception.
- **MAY** identifies optional behavior.

## Repository-grounded P0 finding: the plaintext memory vault

Friday already contains an Obsidian-like projection mechanism in [`friday/memory/__init__.py`](../friday/memory/__init__.py).

`MemoryVault`:

- treats SQLite as the source of truth;
- renders Knowledge Objects into Markdown files;
- includes frontmatter, summary, full content, and entity wikilinks;
- writes atomically through a temporary file and `os.replace`;
- uses stable object-derived filename suffixes;
- removes stale projections;
- stores per-user projections under a configurable `memory-vault` directory.

The worker layer in [`friday/workers/__init__.py`](../friday/workers/__init__.py) schedules `memory_vault_sync` every 300 seconds when the vault is present. The configuration in [`friday/config/__init__.py`](../friday/config/__init__.py) creates `memory_vault_dir` by default under the Friday data directory. The directory is also part of the runtime directory and backup topology.

This design is useful for ordinary local-first operation, human inspection, and portability. It is not automatically acceptable for restricted material.

The current effective storage topology can become:

```text
raw bytes
    +
SQLite records
    +
FTS and vector indexes
    +
plaintext Markdown projection
    +
backup copies of that projection
```

Private file permissions reduce exposure to other unprivileged local users. They do not protect against:

- disk theft without encryption at rest;
- a privileged operator;
- another process under the same service identity;
- accidental backup or synchronization configuration;
- desktop indexing software;
- forensic acquisition;
- compromise of the host account;
- an application with access to the same directory.

### Required decision

The vault MUST become explicitly profile-controlled.

For restricted deployments, one of the following MUST be true:

```text
memory_vault = disabled
```

or:

```text
memory_vault = label-aware
projection target = approved encrypted storage
projection processor = approved trusted component
restricted objects = excluded unless policy explicitly allows projection
```

The following equivalence MUST NOT exist:

```text
canonical knowledge == exportable plaintext note
```

Canonicality describes authority inside Friday. It does not grant permission to create another plaintext representation.

### Recommended configuration contract

```yaml
projection_profiles:
  memory_vault:
    mode: disabled | public_only | policy_filtered | full_owner
    maximum_security_level: internal
    allowed_compartments: []
    target_attestation_required: true
    include_full_content: false
    include_summary: true
    include_entities: true
    include_source_identity: false
```

A restricted profile SHOULD fail startup if the legacy unrestricted vault projector is active.

## The missing governance model

The new plane should consist of three connected but distinct domains.

### 1. Information-flow governance

Answers:

```text
Where can this source, metadata field, derivative, prompt, result, cache entry, or generated file go?
```

It governs:

- security labels;
- compartments;
- permitted processors;
- permitted channels;
- purpose restrictions;
- derivative inheritance;
- serialization boundaries;
- caches, logs, queues, backups, and exports.

### 2. Record authority and lifecycle

Answers:

```text
What kind of record is this, which version is it, and what may Friday claim about it?
```

It governs:

- source authenticity;
- representation type;
- logical document identity;
- versions and amendments;
- drafts, issued records, revocations, expiration, and supersession;
- signature validation;
- independent corroboration;
- evidence lineage groups.

### 3. System assurance

Answers:

```text
What mechanism, test, and runtime evidence prove each promise?
```

It governs:

- deployment profiles;
- startup attestation;
- architecture fitness tests;
- release gates;
- tamper-evident audit checkpoints;
- incident response;
- recovery from a trusted state;
- evidence that physical boundaries match documented boundaries.

These domains must not be collapsed into one status field or one generic policy function.

## Core invariants

The following invariants should guide the implementation.

### Invariant 1: unknown is not public

A newly received source whose classification has not been established MUST be handled at the most restrictive provisional level derived from:

- intake channel;
- user declaration;
- source policy;
- tenant policy;
- deployment profile.

### Invariant 2: every derivative remains governed

Every material derivative MUST inherit an effective label and lineage from all source evidence.

```text
label(derivative) >= join(labels(all source evidence))
```

### Invariant 3: canonical is not authoritative by itself

Promotion to a Knowledge Object MAY establish that Friday should remember and retrieve the material. It MUST NOT silently establish legal validity, official status, signature validity, current effectiveness, or independent corroboration.

### Invariant 4: review, authority, classification, and lifecycle are independent axes

The following dimensions MUST remain separate:

```text
review status
record authority
security classification
record lifecycle
```

### Invariant 5: policy applies before serialization

An outbound operation MUST be authorized before the sensitive body is constructed, serialized, logged, queued, traced, or handed to another process.

### Invariant 6: approval is not declassification

Approval to perform one effect MUST NOT lower the security label or authorize another destination.

### Invariant 7: deletion includes derivative closure

A source is not fully deleted while a searchable, readable, exportable, or restorable derivative remains active outside an explicit retention or legal-hold policy.

### Invariant 8: configuration combinations are part of the security model

Friday MUST validate the effective combination of features, endpoints, mounts, routes, projectors, backup targets, and channels. Independent safe defaults are not enough.

### Invariant 9: the secure path must be operationally usable

A secure workflow that users routinely bypass is not an effective control.

### Invariant 10: architecture claims require evidence

A statement such as "restricted data never leaves Secure Core" is incomplete until it is bound to:

```text
mechanism
executable test
runtime evidence
operator procedure
failure behavior
```

## Record authority ladder

Friday needs an explicit authority ladder. A recommended initial form is:

```text
captured_bytes
    -> safely_parsed_representation
    -> user_provided_source
    -> reviewed_source
    -> canonical_knowledge
    -> authoritative_record
    -> independently_corroborated_fact
```

The ladder is not necessarily linear for every object. It is a vocabulary for claims.

### Level: captured bytes

Friday can claim:

- the bytes were received through a recorded channel;
- a digest was calculated;
- the stored bytes match that digest;
- source and uploader provenance were recorded to the available degree.

Friday cannot yet claim:

- the file is safe;
- the file is complete;
- the visible content was parsed correctly;
- the author is genuine;
- the record is current or valid.

### Level: safely parsed representation

Friday can claim:

- a specified parser revision produced a bounded representation;
- extraction completeness and warnings are recorded;
- active content was not executed;
- the representation is linked to the immutable source digest.

Friday cannot yet claim that extracted statements are true.

### Level: user-provided source

Friday can claim:

- a known actor provided the source;
- the source is accessible within that actor's policy boundary.

The uploader is not automatically the author or issuing authority.

### Level: reviewed source

Friday can claim:

- a person or approved policy reviewed classification, metadata, or suitability;
- the review decision and reviewer identity are recorded.

Review does not automatically establish official authenticity.

### Level: canonical knowledge

Friday can claim:

- the object is eligible for normal memory and retrieval behavior;
- it represents the current preferred internal knowledge projection unless superseded;
- provenance remains available.

Canonical knowledge can still be wrong, incomplete, disputed, or based on a non-authoritative source.

### Level: authoritative record

Friday may claim this only when policy-defined evidence exists, such as:

- trusted source system;
- verified digital signature;
- controlled import;
- confirmed official copy;
- explicit records-management decision;
- current validity state.

### Level: independently corroborated fact

Friday may claim independent corroboration only when evidence comes from distinct lineage groups, not merely multiple representations of the same source.

## Logical document identity and version families

A content digest identifies bytes. It does not identify a legal or logical document across revisions.

Friday should distinguish:

```text
physical_object_id
logical_document_id
document_family_id
version_id
representation_id
evidence_lineage_group_id
```

### Representation type

A proposed vocabulary:

```text
original
electronic_original
signed_original
scan
certified_copy
ordinary_copy
translation
rendered_derivative
extracted_representation
generated_output
```

### Record lifecycle

A proposed vocabulary:

```text
draft
issued
effective
suspended
superseded
revoked
expired
archived
destroyed
unknown
```

### Inter-record relationships

```text
version_of
supersedes
superseded_by
amends
amended_by
annex_to
response_to
duplicate_of
translation_of
rendered_from
extracted_from
derived_from
```

### Required behavior

A retrieval result SHOULD expose lifecycle and version information prominently.

Friday MUST NOT silently present a superseded or revoked record as current merely because it ranks highly for content similarity.

When several versions match, the default result SHOULD group them under one logical document family and identify:

- latest known effective version;
- matched version;
- differences;
- authority and validity uncertainty.

## Evidence lineage groups and independence accounting

One source can produce many physical and semantic representations:

```text
DOCX
    -> PDF rendering
    -> scan
    -> OCR text
    -> summary
    -> translated summary
    -> generated report
```

These are not independent evidence.

Every derivative SHOULD carry an `evidence_lineage_group_id` that identifies the originating evidence family.

Independent-source accounting MUST deduplicate by lineage group before claiming corroboration.

### Examples

```text
three copies of one signed order
    -> one independent source

original contract plus independently issued registry extract
    -> two independent sources

Friday-generated report re-uploaded by the user
    -> still derived from the original source groups
```

A generated artifact MUST retain source lineage even after re-ingestion.

## Digital signatures and authenticity

Signature handling can be implemented incrementally, but the schema should reserve first-class fields now.

A proposed signature record:

```text
signature_id
physical_object_id
format
signer_claim
certificate_subject
certificate_fingerprint
signature_present
cryptographic_signature_valid
certificate_chain_status
revocation_status
trusted_timestamp_status
signed_revision_digest
validation_policy
validated_at
validator_identity
validation_result
```

Important distinctions:

```text
signature present != signature valid
signature valid != certificate trusted
certificate trusted != signer authorized
signer authorized != record currently effective
```

The UI and answer layer SHOULD avoid the generic word "verified" without identifying exactly what was checked.

## Classification lifecycle

Security classification needs a lifecycle, not only a label string.

### Provisional classification before parsing

The system must decide which processors may open a document before it knows its content.

A safe intake sequence is:

```text
source and channel policy
    -> provisional security label
    -> quarantine storage
    -> approved local static inspection
    -> classification proposal
    -> policy or human confirmation
    -> normal processing path
```

Examples:

```text
restricted local intake
    -> provisional restricted

ordinary Telegram intake
    -> maximum channel class according to policy
    -> declared restricted material rejected before normal ingestion

trusted public feed
    -> provisional public under a named source policy
```

### Classification record

```text
classification_id
subject_id
level
compartments
classification_source
classifier_identity
classifier_revision
confidence
confirmed_by
confirmed_at
review_due_at
allowed_purposes
retention_schedule
declassification_rule
legal_hold
destruction_policy
created_at
superseded_by
```

### Separate axes

A document can legitimately have this state:

```text
review_status = pending
record_authority = user_provided_source
security_level = restricted
record_lifecycle = effective
```

or:

```text
review_status = approved
record_authority = canonical_knowledge
security_level = internal
record_lifecycle = superseded
```

No single `status` field should try to represent these combinations.

## Derivative security labels and lifecycle closure

The sensitive-data graph includes more than files.

Friday should label and track:

- raw objects;
- extracted text;
- OCR pages;
- structural indexes;
- passages;
- embeddings;
- rerank inputs and caches;
- titles and summaries;
- tags and facets;
- entities and relations when their existence is sensitive;
- message passages;
- search excerpts;
- generated files;
- Workbench annotations;
- saved view names and queries;
- thumbnails;
- temporary files;
- notification payloads;
- backup manifests.

### Lineage record

```text
object_id
object_kind
source_object_ids
transformation_kind
processor_identity
processor_revision
created_at
security_label_id
content_digest
completeness
superseded_by
invalidated_at
destroyed_at
```

### Reclassification closure

When a source becomes more restrictive, Friday MUST:

- recompute effective labels of derivatives;
- remove disallowed projections;
- invalidate channel-ready payloads;
- invalidate generated export approvals;
- update indexes and caches;
- restrict graph material where necessary;
- mark affected reports and answers stale.

### Deletion closure

When deletion is permitted and requested, Friday MUST account for:

- raw bytes;
- SQLite rows;
- FTS rows;
- passage rows;
- vector rows and resident caches;
- Markdown vault projections;
- generated files;
- thumbnails and temporary renders;
- browser-delivery caches;
- active backups;
- backup retention or legal-hold exceptions;
- graph relations and revision history;
- audit records.

Deletion policy MUST define what remains and why.

## Retention, history, and legal hold

Friday already values append-only history and reproducibility. Sensitive records create a legitimate tension:

```text
preserve historical accountability
    vs
remove material when policy requires destruction
```

The project should define separate policies for:

- operational history;
- records history;
- security audit;
- backup retention;
- legal hold;
- user deletion;
- cryptographic erasure.

### Audit minimization

Audit records SHOULD store shape and decision evidence, not document bodies.

Potentially sensitive names, titles, and relationship values SHOULD be replaced by stable opaque identifiers or digests when the operational purpose allows it.

### Tombstones

A tombstone may preserve:

```text
opaque subject identity
reason code
destruction time
authorized actor
policy identifier
proof digest
```

It SHOULD NOT preserve the deleted content under another field name.

## Deployment profiles

A large set of independent feature flags is insufficient for a sensitive system. Friday should provide named profiles with validated combinations.

### Profile: personal connected

```yaml
profile: personal_connected
external_web: allowed
external_mcp: allowed_by_tool_policy
telegram_files: allowed_by_user_policy
remote_models: configurable
memory_vault: allowed
backup_target: configurable
secure_core_split: optional
```

### Profile: confidential local

```yaml
profile: confidential_local
external_web: public_projection_only
external_mcp: denied_for_confidential_sources
telegram_files: denied_for_confidential_sources
remote_models: denied_for_confidential_sources
memory_vault: policy_filtered
backup_target: encrypted_and_attested
secure_core_split: required_for_confidential_processing
```

### Profile: restricted offline

```yaml
profile: restricted_offline
network_egress: none
telegram_content: none
external_web: none
external_mcp: none
remote_models: none
memory_vault: disabled
local_models: attested_only
local_parsers: attested_and_sandboxed
backup_target: encrypted_offline_only
secure_workbench: required
```

### Startup attestation

Friday MUST validate effective state, not only configuration text.

A startup attestation for sensitive profiles SHOULD inspect:

- active network routes;
- DNS reachability;
- listening and connected sockets;
- container or service mounts;
- document-store accessibility from Network Edge;
- configured model endpoints;
- MCP server definitions;
- backup destinations;
- memory-vault state and path;
- telemetry destinations;
- browser frontend asset origins;
- runtime images and model manifests;
- service identities and file permissions.

A restricted profile MUST fail startup on a contradictory state.

Warnings are not sufficient for release-blocking invariants.

## Assurance case and assurance matrix

Every important security or correctness claim should be represented as an assurance case.

### Claim structure

```text
Claim
    -> mechanism
    -> executable test
    -> runtime evidence
    -> operator procedure
    -> failure behavior
    -> responsible owner
```

### Initial assurance matrix

| Claim | Mechanism | Executable test | Runtime evidence |
|---|---|---|---|
| Restricted content never leaves Secure Core | Network namespace, no egress route, pre-serialization policy | Packet-capture integration test | Startup network attestation |
| Network Edge cannot read documents | No document or database mounts, separate identity | Mount and permission inspection | Signed deployment manifest |
| Restricted data never reaches Telegram | Channel policy before payload construction | End-to-end publication regression | Body-free denial event |
| Restricted data never reaches an external MCP server | Label-aware capability broker | Tool-call serialization test | MCP route decision record |
| Deleted source has no active derivatives | Lineage traversal and closure | Full deletion-closure test | Derivative inventory report |
| Only approved local models process restricted text | Attested endpoints and profile policy | Model identity probe | Runtime model manifest |
| A superseded record is not presented as current | Version-family ranking and answer policy | Version conflict retrieval test | Search explanation trace |
| Independent corroboration uses independent sources | Evidence lineage grouping | Duplicate representation test | Evidence group list |
| Restricted projections are not written to memory-vault | Label-aware or disabled projector | Projection boundary test | Projector profile status |
| Export approval applies to one exact artifact and destination | Digest-bound export object | Mutation-after-approval test | Export receipt and digest |

The matrix should live in code-owned or machine-readable form so that a declared claim without a detector or test fails quality gates.

## Key-management plane

Local processing is not equivalent to encrypted storage.

Sensitive information may exist in:

```text
raw files
SQLite and WAL
FTS index
passage tables
embedding storage
resident vector cache
reranker cache
temporary pages
thumbnails
memory-vault Markdown
generated files
backups
model prompts
KV cache
VRAM
crash dumps
swap
```

Friday needs an explicit key-management design, even if encryption is primarily implemented by the operating system or storage layer.

### Threat model questions

The deployment owner must answer:

- Is disk theft in scope?
- Is root trusted?
- Is the infrastructure operator trusted?
- Is the backup operator trusted?
- Are multiple tenants protected from each other cryptographically or only by application authorization?
- Are compartments expected to survive one tenant credential compromise?
- Is cryptographic erasure required?

### Key hierarchy

A possible hierarchy:

```text
recovery root
    -> deployment key-encryption key
        -> tenant data key
            -> optional compartment key
                -> object or volume encryption keys
```

The exact design depends on the threat model. Friday should not invent a complex hierarchy without a requirement, but it should not leave the decision implicit.

### Required operational contracts

```text
key generation
key storage
unlock procedure
rotation
revocation
backup-key escrow
restore ceremony
operator separation
lost-key response
cryptographic erasure
destruction evidence
```

### Backup and key separation

The data backup and the only recovery key SHOULD NOT be stored together without an additional protection boundary.

The restore procedure MUST be tested on a clean host.

A backup that cannot be decrypted is not a backup. A backup whose key is exposed beside it is not meaningfully protected from the same compromise.

## Stable identity across backup and restore

A long-lived archive needs identities that survive storage migration and disaster recovery.

Friday should distinguish:

```text
content identity
logical record identity
version identity
representation identity
deployment-local row identity
```

External references, Workbench annotations, citations, saved views, and exported manifests SHOULD depend on stable logical identities rather than incidental SQLite row identities where practical.

A restore operation SHOULD produce an identity reconciliation report:

```text
preserved logical identities
new deployment-local identities
unresolved references
relinked annotations
relinked citations
reindexed representations
```

## Parser quarantine and active-content triage

Safe parsing and safe acceptance are different questions.

Friday should record separate verdicts for:

```text
parse safety
malware status
active content
authenticity
external references
semantic trust
```

### Static intake inspection

Before ordinary document processing, the intake gateway SHOULD inspect:

- extension and magic mismatch;
- suspicious polyglot structure;
- archive nesting and expansion ratio;
- macro presence;
- OLE and embedded objects;
- embedded executables;
- PDF JavaScript and actions;
- embedded files;
- external relationships;
- remote templates;
- hidden sheets and slides;
- tracked changes and comments;
- formula injection risk;
- unsupported encryption;
- parser-specific structural anomalies.

### Triage vocabulary

```text
clean_for_static_parse
active_content_detected
quarantined
unsupported_risk
manual_review_required
malware_suspected
```

### Parser sandbox

Parsers SHOULD run:

- without network access;
- under a separate unprivileged identity;
- with read-only source input;
- with an empty bounded writable workspace;
- with CPU, memory, process, and time limits;
- without Friday secrets;
- without the primary database mount;
- under seccomp, AppArmor, SELinux, or an equivalent control where available.

A parser failure MUST NOT trigger an unapproved cloud fallback.

## Supply-chain assurance

Model attestation is valuable, but the trusted processing chain is broader than the main LLM.

The assurance scope SHOULD include:

- parser images and libraries;
- OCR engine;
- PDF renderer;
- Office libraries;
- archive libraries;
- embedding model;
- reranker;
- inference runtime;
- frontend bundle;
- container base images;
- GPU driver and critical runtime components;
- update packages.

Recommended controls:

```text
pinned artifacts
content digests
signed release manifests
SBOM
verified build provenance
offline update procedure
vulnerability response
rollback plan
component attestation at startup
```

A local compromised parser is an external exfiltration risk wearing an indoor coat.

## Trusted inference fabric for 120B+ models

Large models can change the physical system boundary.

A 120B+ model may run:

- on multiple GPUs;
- in several processes;
- on several machines;
- behind an inference server;
- across a high-speed network;
- with prefix and KV caches;
- with a shared scheduler.

Therefore `local model` MUST NOT be used as a sufficient security classification.

### Trusted inference fabric

A sensitive deployment SHOULD define:

```text
isolated inference network
no public internet route
mutually authenticated clients and servers
attested model and runtime identity
no prompt or completion logging
body-free metrics
protected crash handling
bounded cache lifetime
security-label and tenant isolation
controlled worker recycle and GPU reset
```

### Prompt transport

The path between Friday and the model server is part of the sensitive-data boundary.

It SHOULD provide:

- authenticated endpoints;
- encryption in transit where the path crosses a host boundary;
- bounded requests;
- no intermediary proxy logging;
- request correlation without body capture;
- explicit endpoint audience binding.

### Prefix and KV caches

Prefix caches and KV caches MUST be treated as sensitive derivatives.

The design must decide:

- whether caches are shared across tenants;
- whether they are shared across compartments;
- cache retention duration;
- invalidation on reclassification or session end;
- behavior after worker crash;
- whether a lower-class request can ever reuse higher-class cache state.

A safe default for restricted processing is isolation by security domain and no cross-tenant reuse.

### Resource governance

The new hardware will be contested by:

```text
interactive search
long-document analysis
OCR
embeddings backfill
reranking
V12 missions
catalog rebuilds
maintenance jobs
```

Friday needs an inference scheduler policy with:

- interactive priority;
- bounded background concurrency;
- preemption or cooperative yielding;
- tenant quotas;
- per-class workloads;
- maintenance windows;
- degraded local smaller-model routes;
- no cloud fallback.

## Incident-response architecture

A secure system needs a response for suspected compromise, not only ordinary component failure.

### Compromise states

Recommended states:

```text
healthy
suspected_compromise
contained
integrity_unknown
recovery_in_progress
verified_recovery
```

`integrity_unknown` is important. A database that opens successfully after compromise is not automatically trustworthy.

### Minimum incident playbook

```text
1. Detect and classify the signal.
2. Freeze outbound effects.
3. Isolate the affected zone.
4. Revoke sessions, tokens, and service credentials.
5. Rotate secrets and encryption keys according to scope.
6. Preserve body-free forensic evidence.
7. Determine the earliest potentially affected time.
8. Traverse affected derivative lineage.
9. Invalidate caches, projections, and generated outputs.
10. Rebuild on a trusted host from verified artifacts.
11. Restore from a verified clean point.
12. Reclassify uncertain records and results.
13. Run assurance and retrieval batteries.
14. Reopen channels gradually.
```

### Channel freeze

The system SHOULD support a single operational action that disables:

- Telegram publication;
- external web;
- external MCP;
- generated-file delivery;
- remote model calls;
- external exports;
- proactive notifications containing metadata.

Local read-only investigation should remain possible where safe.

## Tamper-evident audit

Friday's logical append-only audit is useful for normal operations. A database-level compromise can make later proof difficult if audit records have no external integrity anchor.

For sensitive profiles, Friday SHOULD add:

```text
monotonic sequence numbers
hash-chained audit batches
signed periodic checkpoints
deployment identity
external or offline checkpoint anchor
body-free event digests
```

The audit chain must not become another content archive.

A checkpoint can prove that a sequence existed without recording document bodies.

### Example audit checkpoint

```text
checkpoint_id
deployment_id
first_sequence
last_sequence
previous_checkpoint_hash
batch_merkle_root
created_at
signing_key_id
signature
```

## Operator and insider threat model

The system must record whom it trusts.

Questions that require explicit answers:

- Is root trusted?
- Is the Friday operator allowed to read document bodies?
- Is the backup operator allowed to restore plaintext?
- Can an administrator know that a document exists without permission to read it?
- Can one tenant infer another tenant's document count, people, projects, or timing?
- Is break-glass access permitted?
- Does break-glass require dual control?

### Authorization model

RBAC may be insufficient for sensitive deployment. ABAC-style conditions may be required:

```text
actor clearance
security level
compartment membership
purpose of use
case assignment
device trust
time-bounded grant
break-glass state
```

### Existence privacy

For some records, existence is sensitive.

The API and UI SHOULD avoid distinguishable responses that reveal:

- a hidden document title;
- a person's presence in another tenant;
- a restricted project name;
- document counts;
- whether access was denied to a specific known identifier.

Depending on policy, `not found` and `not authorized` may require the same external response shape.

## Human review at scale

A stronger catalog and classification model can create a review bottleneck.

Review may be requested for:

- semantic title;
- document type;
- dates and date roles;
- people and organizations;
- relations;
- security classification;
- record authority;
- version and supersession;
- active-content verdict;
- export decisions.

Review should be risk-based.

### Suggested review tiers

```text
trusted source + known template + low sensitivity
    -> automated structured intake
    -> sampled human quality review

unknown source or ambiguous metadata
    -> ordinary review

restricted classification or authority transition
    -> mandatory human review

high-impact export or declassification
    -> dual approval
```

### Review UX requirements

- batch confirmation;
- grouping by source and template;
- review by exception;
- confidence and evidence display;
- queue age and ownership;
- service-level targets;
- sampled quality control;
- reversible decisions;
- no silent promotion after reviewer timeout.

The model MAY propose. It MUST NOT certify its own proposal merely because it is a larger model.

## Secure workflow usability

The secure path must compete with Telegram in convenience.

A practical restricted intake should be:

```text
drag or select file
    -> immediate durable receipt
    -> visible security and processing status
    -> searchable catalog entry
    -> readable local preview
    -> clear warnings and next action
```

A secure workflow that requires many manual steps invites unsafe bypass under time pressure.

### Remote-access decision

Friday should make one explicit product decision:

```text
restricted remote access is forbidden
```

or:

```text
restricted remote access exists only through managed devices and a separately designed secure channel
```

Telegram MUST NOT fill this gap accidentally.

## Controlled export

Export is a high-risk side effect.

Generated DOCX, XLSX, PDF, and other files can contain:

- author metadata;
- application metadata;
- internal paths;
- comments;
- revision history;
- hidden rows, sheets, or slides;
- formulas;
- custom XML;
- embedded files;
- external relationships;
- internal Friday identifiers;
- excessive provenance details.

### Export pipeline

```text
render
    -> sanitize
    -> inspect
    -> classify
    -> compute digest
    -> bind destination
    -> request approval
    -> deliver exact digest
    -> record receipt
```

### Export authorization object

```text
export_id
actor_id
source_object_ids
effective_security_label
artifact_digest
artifact_size
artifact_type
destination
purpose
approval_id
approved_at
expires_at
delivery_status
receipt
```

An approval MUST be invalidated if:

- artifact bytes change;
- destination changes;
- effective classification changes;
- approval expires;
- source access is revoked.

### Approval is destination-specific

```text
approve local generation
    != approve download
    != approve print
    != approve email
    != approve Telegram
    != declassify
```

Declassification, where permitted, MUST be a separate explicit records decision.

## Right to exit and archival portability

A backup protects against machine failure. It does not protect the owner from loss of the Friday implementation itself.

Friday SHOULD provide an offline, documented archival export containing:

```text
original bytes
content digests
logical document identities
version families
record lifecycle
security labels and compartments
classification history
typed dates and facets
annotations
provenance
evidence lineage
relations
human-readable index
machine-readable manifest
schema version
```

The export MUST preserve security labels and SHOULD be encryptable without an external service.

The format SHOULD be readable independently of a particular Friday release.

This is not the same as the memory-vault Markdown projection. A durable archival package needs complete identity, lineage, version, and policy metadata.

## Architecture fitness and complexity budget

Friday's test suite demonstrates a strong invariant-driven culture. At the same time, several central modules have become very large. Adding DocumentCatalog, V12, MCP routing, security labels, the Workbench, export governance, and inference scheduling directly into existing central modules would increase change blast radius.

A new rule is recommended:

> Each architectural phase should reduce the responsibility of central runtime modules, not only add behavior to them.

### Stable domain boundaries

Suggested domains:

```text
records
classification
lineage
retrieval
inference
channels
exports
assurance
workbench
```

### Fitness tests

Architecture tests SHOULD enforce:

- forbidden imports between domains;
- no channel adapter importing raw storage internals;
- no external tool receiving unclassified evidence;
- no projector bypassing classification policy;
- no model runtime deciding authorization;
- no UI route reading physical source paths directly;
- no generated-file delivery bypassing the export broker;
- no backup module omitting a declared governed store;
- no declared security claim without a detector and test.

### Ports and adapters

V12, MCP, Telegram, and Workbench should call stable application contracts rather than storage mixins directly.

The execution shape should remain:

```text
interface
    -> application contract
    -> policy and authorization
    -> domain operation
    -> projection or effect
    -> assurance evidence
```

## Target architecture

```text
                               User Interfaces
                 Telegram | Secure Workbench | Local API
                                  |
                                  v
                      Application Contract Layer
                                  |
               +------------------+------------------+
               |                  |                  |
               v                  v                  v
          Archive Search     Records Service    Mission Runtime
               |                  |                  |
               +------------------+------------------+
                                  |
                         Policy and Assurance
              classification | authority | lineage | effects
                                  |
             +--------------------+--------------------+
             |                    |                    |
             v                    v                    v
        Secure Core          Guarded Broker       Network Edge
   storage, parsers, models   typed crossings    Telegram, web, MCP
             |
             v
      Attested local stores and processors
```

The model is a consumer of permitted context and tools. It is not the policy authority, record certifier, key manager, or assurance witness.

## Implementation sequence

### Phase 0: immediate containment and decisions

- Make `memory-vault` profile-controlled.
- Disable unrestricted Markdown projection in restricted deployments.
- Record the trusted-operator and root threat model.
- Choose the initial classification taxonomy.
- Define provisional classification by intake channel.
- Define whether restricted remote access is forbidden or separately supported.
- Record the encryption and key-recovery assumptions.

### Phase 1: classification and lineage foundations

- Add typed security labels.
- Add compartments and allowed-purpose fields.
- Add source-to-derivative lineage.
- Add effective-label joins.
- Label passages, vectors, generated files, annotations, and projections.
- Implement reclassification and deletion closure.
- Add an inventory that explains all active derivatives of a source.

### Phase 2: record authority and versioning

- Add logical document and family identities.
- Add representation types.
- Add lifecycle and supersession.
- Add evidence lineage groups.
- Group search results by document family.
- Prevent superseded records from appearing as current without warning.
- Reserve signature validation schema.

### Phase 3: deployment profiles and assurance

- Add named deployment profiles.
- Validate actual routes, mounts, endpoints, projectors, and backup targets.
- Create the machine-readable assurance matrix.
- Bind claims to mechanisms, tests, and runtime evidence.
- Fail startup on restricted-profile contradictions.

### Phase 4: Secure Core and guarded effects

- Physically separate Secure Core and Network Edge.
- Centralize Telegram, web, MCP, remote model, and export effects.
- Authorize before serialization.
- Ensure Network Edge has no source-store mount.
- Add emergency channel freeze.

### Phase 5: parser quarantine and supply chain

- Add active-content triage.
- Sandbox document parsers.
- Pin parser and OCR artifacts.
- Produce signed component manifests and SBOMs.
- Add controlled offline update and rollback procedures.

### Phase 6: trusted inference fabric

- Define the 120B+ cluster security boundary.
- Attest model, runtime, proxy, and launch configuration.
- Isolate network and caches.
- Add scheduling priorities and quotas.
- Prohibit cloud fallback.
- Add cache lifecycle and GPU-worker reset procedures.

### Phase 7: incident response and tamper evidence

- Add compromise states.
- Add a tested containment runbook.
- Add hash-chained audit checkpoints.
- Add clean-host recovery procedure.
- Add post-restore assurance and retrieval batteries.

### Phase 8: controlled export and portability

- Add digest-bound export authorizations.
- Sanitize generated Office and PDF artifacts.
- Add destination-bound delivery receipts.
- Add offline archival package format.
- Test independent readability and restore.

### Phase 9: architecture decomposition

- Extract stable domain services from central runtime modules.
- Add forbidden-import and surface-area fitness tests.
- Keep V12 and MCP behind application contracts.
- Reduce direct storage access from channels and UI.

## Priority table

| Priority | Work item | Reason |
|---|---|---|
| P0 | Disable or make current plaintext `memory-vault` label-aware | Existing second plaintext representation |
| P0 | Define deployment profiles and startup attestation | Prevent unsafe feature combinations |
| P0 | Define provisional classification before parsing | Processor choice occurs before content understanding |
| P0 | Define key, backup, restore, and cryptographic-erasure model | Local plaintext stores and backups need a threat model |
| P1 | Add authority ladder and document version families | Retrieval must know which record is current and authoritative |
| P1 | Add evidence lineage groups | Prevent false independent corroboration |
| P1 | Build parser quarantine and active-content triage | Static parse safety is not malware safety |
| P1 | Define trusted inference fabric before 120B+ deployment | Large models introduce new physical boundaries and caches |
| P1 | Add compromise-response runbook and tamper-evident checkpoints | Ordinary recovery is not compromise recovery |
| P2 | Scale risk-based review workflows | Metadata and classification review can become the bottleneck |
| P2 | Implement controlled export | Output files are a major exfiltration path |
| P2 | Provide portable archival export | Long-term owner independence |
| P2 | Enforce architecture fitness | Prevent central runtime modules from absorbing every new plane |

## Release gates

A sensitive deployment MUST NOT be declared ready until the following are demonstrated.

### Information flow

- Restricted processing produces no unauthorized outbound network traffic.
- Restricted data cannot reach Telegram, external MCP, public web, remote models, cloud OCR, or body telemetry.
- Authorization occurs before serialization.
- Every material derivative carries an effective label and lineage.
- The memory-vault projector cannot write material above its configured maximum level.

### Records governance

- Search distinguishes matched version from current effective version.
- Superseded and revoked records are visibly marked.
- Independent corroboration counts lineage groups, not copies.
- Review status, authority, classification, and lifecycle are separate.
- Signature claims identify the exact validation performed.

### Storage and keys

- Sensitive stores are covered by the encryption and key-management design.
- Backup and recovery keys are protected by a separate boundary.
- Restore succeeds on a clean host.
- Stable logical identities and annotations survive restore or produce an explicit reconciliation report.
- Cryptographic erasure behavior is documented and tested where required.

### Parsing and supply chain

- Parsers run without network and without Friday secrets.
- Active content is detected and never executed.
- Unsupported-risk files enter quarantine rather than ordinary ingestion.
- Processing artifacts are pinned and attestable.
- An update rollback has been exercised.

### Inference

- Model servers have no public route in restricted profile.
- Model and runtime identity are attested.
- Prompts and completions are not logged.
- Cache isolation and retention are explicit.
- Background indexing cannot starve interactive work.
- No cloud fallback exists.

### Incident response

- One operation freezes outbound effects.
- Suspected compromise can move the deployment into `integrity_unknown`.
- Recovery uses verified artifacts and a verified clean point.
- Audit checkpoints detect sequence modification.
- Post-recovery assurance and retrieval batteries pass before reopening channels.

### Export and portability

- Export approval is bound to one digest and one destination.
- Artifact mutation invalidates approval.
- Generated Office and PDF outputs are inspected and sanitized.
- An offline archival package can be read without the running Friday instance.
- Security labels survive export.

## Suggested regression tests

Friday's human-readable invariant naming style is well suited to this plane.

```text
test_a_restricted_object_never_enters_the_memory_vault.py
test_a_projection_profile_is_checked_before_markdown_exists.py
test_unknown_is_never_treated_as_public.py
test_a_derivative_inherits_the_strictest_source_label.py
test_a_reclassification_closes_every_old_projection.py
test_a_deleted_source_leaves_no_searchable_derivative.py
test_three_formats_of_one_record_are_one_evidence_group.py
test_a_superseded_record_is_not_called_current.py
test_a_valid_signature_is_not_called_a_trusted_author.py
test_review_authority_classification_and_lifecycle_are_separate.py
test_a_restricted_profile_refuses_an_external_route.py
test_a_restricted_profile_refuses_an_unfiltered_vault.py
test_the_network_edge_has_no_document_mount.py
test_a_policy_denial_happens_before_serialization.py
test_a_parser_has_no_network_or_secrets.py
test_active_content_enters_quarantine.py
test_a_cloud_parser_fallback_fails_closed.py
test_a_restricted_prompt_reaches_only_an_attested_local_model.py
test_a_prefix_cache_never_crosses_security_domains.py
test_background_backfill_cannot_starve_an_interactive_search.py
test_an_incident_freeze_closes_every_outbound_channel.py
test_integrity_unknown_is_not_reported_as_healthy.py
test_an_audit_checkpoint_detects_rewritten_history.py
test_an_export_approval_is_bound_to_one_digest.py
test_changing_the_destination_requires_a_new_approval.py
test_an_approval_never_declassifies_the_record.py
test_a_portable_archive_preserves_logical_identity_and_labels.py
```

Deployment tests should additionally inspect:

```text
network namespaces
firewall rules
DNS reachability
container mounts
service identities
open ports
configured endpoints
backup destinations
memory-vault mode
telemetry sinks
model manifests
frontend remote-resource policy
```

## Operator checklist

Before admitting sensitive production material:

- [ ] The classification taxonomy and provisional intake rules are approved.
- [ ] The trusted-operator and root model is written down.
- [ ] A named deployment profile is selected.
- [ ] `memory-vault` behavior is explicitly configured and verified.
- [ ] Secure Core egress and Network Edge mounts are tested.
- [ ] Local parsers and models are approved and attested.
- [ ] Prompt, output, and body logging are disabled.
- [ ] Backup encryption and key recovery are tested.
- [ ] Restore on a clean host has succeeded.
- [ ] Incident freeze and recovery procedures have been rehearsed.
- [ ] Controlled export is implemented or disabled.
- [ ] The Secure Workbench supports the required restricted workflow.
- [ ] The assurance matrix has no unowned release-blocking claim.

## Relationship to the other architecture observations

### MCP architecture

MCP remains appropriate for external integration plumbing. The assurance plane determines which MCP capability is available for a given evidence set and deployment profile.

External MCP MUST NOT receive restricted evidence by default.

### Document and message retrieval

DocumentCatalog, typed dates, unified archive search, pending-source recall, and message passages remain necessary.

The records plane adds:

- logical document families;
- version and supersession;
- authority;
- evidence independence;
- security-aware result projection.

Better recall must not make unauthorized or obsolete material easier to present incorrectly.

### Secure Workbench

The Workbench remains the primary interface for high-sensitivity work.

The assurance plane adds:

- trusted deployment state;
- label-aware views;
- authority and lifecycle display;
- derivative inventory;
- incident state;
- controlled export;
- secure session behavior.

### V12 and 120B+ models

V12 should plan over typed retrieval, record authority, and policy-constrained tools.

Larger models can improve classification proposals, metadata extraction, reranking, and synthesis. They do not replace:

- record identity;
- policy;
- lineage;
- deployment isolation;
- key management;
- assurance evidence.

## Final recommendation

Friday does not primarily lack another feature. It lacks a single explicit plane that connects every promise to the complete life of the information.

The recommended organizing model is:

```text
Information-flow governance
    -> decides where data and derivatives may go

Record authority and lifecycle
    -> decides what Friday may claim about a record

System assurance
    -> proves that the implementation and deployment enforce both
```

The first action should be concrete: make the existing plaintext `memory-vault` projector profile-controlled and classification-aware before restricted records enter production.

The next actions should establish provisional classification, derivative lineage, named deployment profiles, key recovery, record version families, parser quarantine, and the trusted inference fabric.

Once these are present, DocumentCatalog, unified retrieval, Secure Workbench, MCP adapters, V12, and the 120B+ model fleet become parts of one governable system rather than individually strong components connected by assumptions.

Friday already has many rooms. This document defines the building code, the records office, the fire doors, and the inspection certificate.

## Repository references

- [`pyproject.toml`](../pyproject.toml)
- [`friday/memory/__init__.py`](../friday/memory/__init__.py)
- [`friday/workers/__init__.py`](../friday/workers/__init__.py)
- [`friday/config/__init__.py`](../friday/config/__init__.py)
- [`friday/storage/models.py`](../friday/storage/models.py)
- [`friday/storage/`](../friday/storage/)
- [`friday/ingestion/`](../friday/ingestion/)
- [`friday/documents/`](../friday/documents/)
- [`friday/generated_files.py`](../friday/generated_files.py)
- [`friday/evidence_bundle.py`](../friday/evidence_bundle.py)
- [`friday/execution_kernel/`](../friday/execution_kernel/)
- [`friday/mcp_runtime/`](../friday/mcp_runtime/)
- [`friday/v12_model_runtime.py`](../friday/v12_model_runtime.py)
- [`friday/v12_model_transport.py`](../friday/v12_model_transport.py)
- [`docs/SECURITY.md`](../docs/SECURITY.md)
- [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)
- [`docs/BACKUP_AND_RESTORE.md`](../docs/BACKUP_AND_RESTORE.md)
- [`docs/DATA_LIFECYCLE.md`](../docs/DATA_LIFECYCLE.md)
- [`docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md`](../docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md)
