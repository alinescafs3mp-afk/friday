# Friday Document and Message Retrieval Audit

> Status: external architecture audit  
> Repository snapshot: `main`, Friday `0.205.0`, 20 August 2026  
> Scope: document registration, tagging, date handling, document discovery, semantic retrieval, message-history search, and the expected impact of MCP, V12, and future 120B+ models.

## Executive conclusion

MCP will not materially fix this problem. MCP can import a file from Drive, mail, a database, or another service, but it does not decide how Friday should register, classify, index, retrieve, date, rank, and explain that material afterward.

A 120B+ model and the V12 model-first runtime will improve:

- query understanding;
- search-plan construction;
- date-role interpretation;
- title, tag, and facet generation;
- difficult reranking;
- cross-document synthesis;
- grounded explanation of retrieved content.

They will not fix the root cause when:

- a document is absent from the searchable candidate set;
- a pending document has no semantic index;
- date roles are conflated;
- an old object falls outside a bounded retrieval pool;
- embeddings are disabled, incomplete, stale, or capped;
- message history uses a separate and much simpler retrieval path;
- the model must guess which of several incompatible search tools represents the user's intent.

The central diagnosis is:

> Friday already has a capable hybrid retrieval engine, but it is placed behind several disconnected corpora, incompatible date semantics, and different admission rules. The main failure is candidate availability and query planning, not answer generation.

The most valuable next investment is not MCP and not a larger chat model. It is a unified, rebuildable document and conversation catalog with complete passage indexing, typed dates, explicit authority states, and one logical archive-search contract.

## Audit limitations

This audit is based on the repository code and checked-in configuration examples. It did not inspect the private production SQLite database, private `.env` values, running embedding service, running reranker, or live index coverage.

Therefore this document can identify architectural failure modes and likely operational risks, but it does not assert that a particular production option is currently enabled or disabled.

Important defaults in the checked-in configuration are nevertheless relevant:

- embeddings are disabled unless explicitly configured;
- reranking is disabled when `FRIDAY_RERANK_TOP=0`;
- the lexical fuzzy pool is bounded;
- dense fallback paths have configurable object and chunk caps;
- resident dense caching removes some of those scan windows only when that path is active and healthy.

## Current retrieval topology

Friday currently has three materially different search worlds.

```text
Uploaded file
  -> Raw Object / Inbox
       -> source_search
          lexical search over source text
          semantic search only through a promoted Knowledge Object

  -> after review and promotion
       -> Knowledge Object
          -> memory_search
             HybridSearcher:
             FTS + morphology + fuzzy lexical + embeddings
             + passage recall + graph + field signals + reranker

Message
  -> messages table
       -> message_search
          FTS / LIKE over individual messages
```

This topology is visible across:

- [`friday/ingestion/_files.py`](../friday/ingestion/_files.py);
- [`friday/storage/_intake.py`](../friday/storage/_intake.py);
- [`friday/storage/_knowledge.py`](../friday/storage/_knowledge.py);
- [`friday/storage/_conversations.py`](../friday/storage/_conversations.py);
- [`friday/retrieval/`](../friday/retrieval/);
- [`friday/execution_kernel/__init__.py`](../friday/execution_kernel/__init__.py).

Each layer is defensible in isolation. The product failure emerges from the gaps between them.

## Finding 1: review state and discoverability are incorrectly coupled

`memory_search` searches promoted Knowledge Objects. `source_search` reaches Raw source text, including pending Inbox material, but its semantic lane starts from HybridSearcher results and therefore from promoted Knowledge Objects.

This creates a common failure sequence:

1. A user uploads a readable document.
2. Friday stores the file and places it in Inbox review.
3. The document is not yet a Knowledge Object.
4. The user later describes it approximately rather than quoting it exactly.
5. `memory_search` cannot see it because it is not promoted.
6. `source_search` can only find it lexically because its semantic candidates also originate from promoted knowledge.
7. Friday may conclude that the material is absent.

This is not primarily a ranking defect. The expected document is absent before ranking begins.

### Required correction

Review and discoverability must become separate concepts.

Review answers:

> May this source be treated as canonical knowledge?

Discoverability answers:

> May the owner locate an authorized source that they uploaded?

A pending document should be semantically retrievable by its authorized owner while remaining explicitly non-canonical:

```text
authority = pending_source
review_status = pending
canonical = false
verification_eligible = false
```

The user-facing response can then say:

> I found a similar uploaded file. It is still awaiting review, so I am treating it as source material rather than confirmed knowledge.

This preserves the review boundary without turning Inbox into a semantic black hole.

## Finding 2: the hybrid search engine is not the primary weakness

[`friday/retrieval/`](../friday/retrieval/) already contains substantial and thoughtful machinery:

- SQLite FTS;
- Russian morphology and stemming;
- keyboard-layout and bounded typo repair;
- fuzzy lexical ranking;
- optional whole-document embeddings;
- passage-level embeddings for long objects;
- graph signals;
- title, summary, tag, entity, and exact-phrase field signals;
- optional cross-encoder reranking;
- explain traces;
- explicit reporting of candidate and dense caps;
- matched-at-least accounting;
- query-aware passage selection.

There are many measured comments in the code showing that this system was tuned against real retrieval failures rather than assembled as a generic RAG demo.

Rewriting HybridSearcher from scratch would therefore be a poor first move.

The larger issue is that different data classes reach different subsets of that engine.

## Finding 3: bounded candidate pools make old semantic material fragile

The fuzzy lexical path uses a bounded pool of important or recent Knowledge Objects. Exact FTS may still find a rare literal term across the index, but a paraphrase of an old document depends heavily on dense recall.

This creates a likely pattern:

```text
exact phrase             -> often found
same fact, new wording   -> depends on embeddings
old document             -> depends on dense coverage and scan strategy
pending document         -> no semantic recall
approximate date         -> depends on upstream planning
message-history paraphrase -> no dense recall at all
```

The repository already contains diagnostics that warn when:

- embedding coverage is below the Knowledge Object count;
- dense object or chunk windows are near their cap;
- the embedding endpoint is unavailable;
- the configured embedding model is not served.

This observability is valuable, but the product still needs a full-corpus semantic retrieval architecture rather than relying on a newest-N fallback scan.

### Recommended direction

Use one of:

- a complete resident vector matrix for the authorized corpus;
- an approximate-nearest-neighbor index;
- a dedicated local vector database with Friday-owned authorization and provenance projection.

The stored vector revision must include the embedding model identity and chunking policy. A model or chunk-policy change should make incompatibility explicit and trigger a resumable backfill.

## Finding 4: physical file registration and searchable registration are different contracts

The repository already has a useful physical registry auditor:

- [`tools/audit_file_registry.py`](../tools/audit_file_registry.py).

It checks:

- relative and safe stored paths;
- file existence;
- content digest;
- metadata consistency;
- symlink and traversal hazards;
- alias conflicts;
- uploader provenance;
- missing or mismatched bytes.

This proves that the file bytes are registered safely.

It does not prove that the user can find the file later.

A document may be physically valid while still lacking:

- a catalog entry;
- a useful semantic title;
- typed document dates;
- passage rows;
- embedding coverage;
- a current enrichment revision;
- a retrievable pending-source representation;
- evidence-bearing excerpts;
- a stable alias set.

### Required new audit

Add a separate read-only tool:

```text
tools/audit_document_catalog.py
```

It should report, without exposing private bodies:

```text
registered_files
catalogued_files
files_with_semantic_title
files_with_passages
files_with_current_embeddings
files_with_typed_dates
pending_files_with_semantic_index
files_with_stale_enrichment_revision
files_with_index_incomplete_reason
files_excluded_by_policy
```

Physical registration and semantic discoverability should be independently observable.

## Finding 5: ordinary files discard the best available title

During enrichment, Friday computes a generated content title and summary. Later in the ordinary file path, the final `KnowledgeEnrichment` title is normally replaced with the original filename. A content-derived title is retained mainly when vision supplies one.

This means documents frequently enter the strongest title-weighted retrieval field under names such as:

```text
scan_014.pdf
Document1.docx
Copy final 2.xlsx
New document.docx
```

Even when the extracted content could support titles such as:

```text
Equipment Acceptance Act under Contract No. 17
Communications Department Staffing Schedule
March 2024 Power Failure Report
```

Hybrid retrieval gives title matches significant weight, so this replacement weakens one of its most trusted signals.

### Required data model

Store these fields separately:

```text
filename_original
filename_normalized
filename_aliases
semantic_title
visible_document_title
```

Their roles differ:

- exact filename queries strongly boost `filename_original`;
- approximate navigation uses aliases, transliteration, trigram similarity, and safe typo matching;
- content queries use `semantic_title`;
- a formal title visible inside the source becomes `visible_document_title` with an evidence span.

A single overloaded `title` field should not represent all four concepts.

## Finding 6: current tags are useful heuristics but weak archival facets

The deterministic enrichment path already improves tags by:

- removing stopwords;
- considering corpus frequency;
- folding word forms;
- removing person-name fragments;
- adding document-kind tags;
- adding selected entity names.

These improvements are valuable.

The remaining limitations are structural.

### Limitation 1: tags are usually isolated words

Documents are better retrieved through phrases and typed facets such as:

```text
equipment acceptance
early mortgage repayment
unit commander
power-supply failure
backup policy
```

than through isolated words such as:

```text
equipment
repayment
commander
failure
backup
```

### Limitation 2: ranking is lost before truncation

The enrichment path eventually deduplicates through a set, sorts alphabetically, and applies a hard limit. If more tags exist than the limit, alphabetic order can determine which survive rather than usefulness or confidence.

### Recommended replacement

Treat human-visible tags as a projection over typed facets rather than as the primary search index.

```text
doc_type: act
topic: equipment acceptance
person: Ivanov Sergey
organization: North LLC
project: Friday
place: Kazan
classification: internal use
```

Each facet should include:

```text
value
normalized_value
type
confidence
source
evidence_span
extractor_revision
```

The UI may display the top 16. The storage and search index should not destroy all remaining facets merely because the screen is small.

## Finding 7: date semantics are the largest conceptual defect

Friday currently deals with several different notions of time, but document search often compresses them into too few fields.

Relevant time roles include:

```text
received_at
container_created_at
container_modified_at
visible_issue_date
visible_registration_date
visible_signing_date
email_sent_at
event_date
mentioned_date
message_created_at
```

These are not interchangeable.

### Problem 1: `document_date` may be a container property

The parser can derive a document date from:

- Office core `created` or `modified`;
- PDF `/CreationDate` or `/ModDate`.

Those values are useful provenance signals, but they are not necessarily the visible legal or business date of an act, order, report, agreement, or letter.

A DOCX creation timestamp may describe:

- template creation;
- first save;
- conversion;
- copying from an older document;
- editor behavior unrelated to the visible document date.

The repository's document release criteria already state the correct rule: container creation and modification dates must be labeled as container properties, not as the legal or visible document date.

### Problem 2: date-window filtering combines own date and any mentioned date

The current knowledge-window predicate may admit a document when either:

- its own stored `document_date` matches, or
- any date listed in metadata `dates` matches.

Thus a query for "documents from March" can mix:

- documents created in March;
- documents signed in March;
- documents uploaded in March;
- documents that merely mention March;
- later documents discussing a historical March event.

No reranker can fully repair a semantically incorrect pre-filter.

### Problem 3: natural-language date interpretation happens before the tool contract

`memory_search` expects normalized `since` and `until` values. `source_search` and `message_search` do not expose equivalent temporal parameters. Therefore the model must correctly infer all of the following before it receives candidates:

1. which corpus the user intends;
2. which date role the wording refers to;
3. the exact normalized interval;
4. which tool supports that interval.

A 120B model will make fewer mistakes than a smaller model, but this remains an unsafe implicit contract.

## Required temporal model

Store typed temporal facts:

```text
document_temporal_facts
  object_id
  role
  start
  end
  precision
  approximate
  source
  confidence
  evidence_span
  extractor_revision
```

Example exact constraint:

```python
TemporalConstraint(
    role="visible_issue_date",
    start="2024-03-01",
    end="2024-03-31",
    precision="month",
    strictness="hard",
)
```

Example approximate constraint:

```python
TemporalConstraint(
    role="received_at",
    center="2024-05-15",
    radius_days=45,
    precision="approximate",
    strictness="soft",
)
```

A soft temporal constraint should normally contribute a ranking score with decay rather than remove all candidates through a hard SQL filter.

### Expected interpretation examples

```text
"What did people send me in May?"
    -> received_at

"Orders dated May"
    -> visible_issue_date

"The file I uploaded sometime last summer"
    -> received_at, soft

"A document that mentioned the March report"
    -> mentioned_date

"Our chat about the server last autumn"
    -> message_created_at + semantic text query
```

When the date role is genuinely ambiguous and materially changes the answer, Friday should expose the distinction rather than silently guessing:

```text
By arrival date: 4 documents
By visible document date: 7 documents
```

## Finding 8: message history uses a much weaker retrieval stack

[`friday/storage/_conversations.py`](../friday/storage/_conversations.py) currently searches individual messages using:

- FTS terms combined with OR;
- prefix matching;
- optional `conversation_id`;
- BM25 plus recency ordering;
- a fallback `LIKE` over the complete query string.

It does not use:

- semantic embeddings;
- a reranker;
- temporal constraints;
- typo repair;
- keyboard-layout repair;
- the same morphological path as document retrieval;
- surrounding message context;
- conversational passage windows;
- role filters;
- person and channel facets.

Therefore a query such as:

> We discussed intermittent test failures caused by parallel execution.

is not guaranteed to find a message phrased as:

> The flaky CI root cause was a teardown race between workers.

Document retrieval treats this as a semantic case. Message retrieval currently treats it as a largely literal case.

## Required message-history index

Index conversational passages rather than only isolated messages.

A passage can represent:

```text
user message + assistant reply
```

or:

```text
previous message + matched message + next one or two messages
```

Suggested structure:

```text
conversation_passages
  passage_id
  conversation_id
  message_ids
  person_id
  started_at
  ended_at
  roles
  text
  embedding_revision
```

A result should return:

- the matched message;
- enough adjacent context to make it meaningful;
- conversation title;
- local timestamp;
- stable message IDs;
- semantic and lexical match metadata.

Otherwise a reply such as "yes, that fixed it" may rank well while failing to reveal what was fixed.

## Finding 9: the model must choose among too many incompatible search tools

The execution kernel exposes separate tools for:

- canonical knowledge search;
- source search;
- message search;
- event history;
- upcoming items;
- entity lookup;
- file collection.

These distinctions are useful internally, but they force the model to choose a corpus before seeing evidence.

The user often does not know which corpus contains the answer:

> I think I either uploaded a file about it or we discussed it in chat sometime last year.

No single current tool naturally represents this request.

## Recommended logical contract: `archive_search`

Retain specialized internal engines, but expose one high-level model contract:

```python
SearchPlan(
    corpora=["documents", "messages", "knowledge"],
    text_query="intermittent test failures during parallel execution",
    temporal_constraint=...,
    uploader=None,
    document_type=None,
    review_states=["canonical", "pending"],
    intent="locate",
)
```

The code, not the model, should then fan out to the appropriate authorized lanes and merge results.

Existing tools can remain for compatibility and narrow expert calls. They should become lower-level capabilities rather than the only way the model can search.

## Recommended target architecture

### Add a rebuildable `DocumentCatalog`

Do not collapse Raw Objects, Inbox, and Knowledge Objects into one table. Their lifecycle semantics are valuable.

Retain:

- Raw Object as immutable source registration;
- Inbox as review state;
- Knowledge Object as confirmed knowledge.

Add a rebuildable search projection:

```text
document_catalog
  raw_object_id
  knowledge_object_id nullable
  tenant_id
  uploader_id

  filename_original
  filename_normalized
  filename_aliases

  semantic_title
  visible_document_title
  document_type

  mime_type
  extension
  received_at

  review_status
  canonical
  evidence_authority
  source_complete

  content_hash
  text_hash

  enrichment_revision
  passage_index_revision
  embedding_revision
```

Add supporting tables or equivalent projections:

```text
document_temporal_facts
document_facets
document_passages
conversation_passages
```

A pending file should receive catalog and passage rows immediately after safe extraction. Promotion changes authority and links the catalog to a Knowledge Object; it does not create discoverability from nothing.

### Core invariant

> Review state changes how confidently Friday may use a source. It must not prevent an authorized owner from locating that source.

## Recommended retrieval pipeline

```text
1. Authorization and lifecycle filtering
   tenant / uploader / privacy / deleted / ignored / pending policy

2. Exact identity recall
   exact filename / alias / document number / message id / exact code

3. Lexical recall
   FTS or BM25 over titles, facets, messages, and passages

4. Approximate identity recall
   character trigrams / typo / layout / transliteration / partial filename

5. Dense recall
   full authorized passage corpus
   including pending Raw documents and conversation passages

6. Rank fusion
   stable reciprocal-rank or calibrated fusion across channels

7. Cross-encoder reranking
   top 40 to 100 candidates

8. Reauthorization
   recheck Raw identity, uploader, lifecycle, privacy, and source bytes

9. Evidence projection
   matched passage, neighboring section or conversation context,
   authority, and completeness
```

The current source-search implementation already contains a strong version of step 8: semantic ranking does not itself authorize evidence. The candidate must be adopted back through the canonical authorized Raw source before its text is published. That property should be preserved.

## MCP impact

MCP is not the remedy for this contour.

Useful MCP roles are:

- import documents from external services;
- access external databases;
- publish generated outputs;
- connect a remote search service if Friday later chooses one.

The following must remain Friday-owned:

- catalog design;
- passage indexing;
- temporal facts;
- candidate generation;
- ranking and fusion;
- authority states;
- provenance;
- review policy;
- reauthorization;
- evidence projection.

An MCP server can transport a document. It cannot define Friday's memory semantics.

## Expected value of 120B+ models

A large model should be used for:

- building a typed `SearchPlan`;
- determining likely date role;
- generating semantic titles;
- extracting keyphrases and typed facets;
- extracting visible requisites with evidence spans;
- reranking difficult ambiguous result sets;
- comparing and synthesizing selected documents;
- background re-enrichment of the existing corpus.

It should not be used for:

- scanning the complete archive on every request;
- replacing embeddings;
- replacing an index;
- authorization;
- proving completeness;
- silently inventing missing metadata;
- declaring absence from a partial candidate set.

More context is not a substitute for retrieval. Even strong long-context models can use relevant information unevenly depending on where it appears in the context. Loading a large arbitrary document set into a 120B model is therefore expensive and still less reliable than retrieving a small evidence-bearing set first.

## Recommended hardware allocation

```text
120B+ reasoning model
    -> planning, interpretation, comparison, synthesis

Dedicated embedding model
    -> continuous full-corpus semantic indexing and recall

Dedicated reranker
    -> ordering a bounded candidate set

Background workers
    -> versioned catalog, passage, date, and facet backfill

RAM or ANN index
    -> full passage corpus without a newest-N boundary
```

A specialized embedding or reranking model is usually a better retrieval investment than spending all available compute on the chat model.

## Expected role of V12

V12 should orchestrate:

```text
user request
    -> typed SearchPlan
    -> bounded authorized retrieval
    -> EvidenceBundle
    -> synthesis or action
```

It should not be expected to perform:

```text
user request
    -> large model somehow searches everything by itself
```

The repository's own V12 decision document already treats storage, retrieval, authorization, and provenance as shared system infrastructure. It also identifies a fast file catalog by people, dates, types, and tags as a real missing capability.

V12 can greatly improve the semantic planner once that planner has a trustworthy catalog and search contract to call.

## Implementation sequence

### P0: measure where candidates disappear

Build a retrieval gold set from actual failure classes, not only synthetic examples.

Each case should record:

```text
query
expected corpus
expected object or message
expected passage
expected date role
acceptable alternatives
```

Required classes:

- approximate content;
- approximate date;
- old file;
- pending file;
- unhelpful filename;
- typo;
- person plus topic;
- topic plus month;
- message-history paraphrase;
- unknown corpus, where the user does not remember whether it was a file or chat.

Measure separately:

```text
catalog coverage
passage-index coverage
embedding coverage
candidate recall@50
candidate recall@100
MRR@10
nDCG@10
grounded-answer accuracy
false-absence rate
date-role accuracy
```

Add one unified `search_explain` output showing:

```text
selected corpora
parsed date role and range
number of authorized objects
which channel recalled each candidate
which caps fired
what was excluded and why
which indexes were absent or stale
```

End-to-end answer quality alone cannot identify whether the failure was planning, recall, reranking, evidence projection, or synthesis.

### P1: separate catalog visibility from promotion

First major product change:

- create `document_catalog`;
- store `semantic_title` separately from filename;
- retain filename aliases;
- create passage rows for every authorized text-bearing Raw file;
- expose review authority labels;
- make the projection versioned and rebuildable;
- allow semantic retrieval of pending documents.

This is likely to produce a larger practical improvement than installing the 120B model immediately.

### P2: build the temporal plane

- add typed date roles;
- store evidence spans;
- support hard and soft constraints;
- share one temporal parser across documents, knowledge, and messages;
- add temporal parameters to source and message search;
- prohibit silent substitution between container, arrival, visible, and mentioned dates.

### P3: complete semantic retrieval

- verify the embedding endpoint and model identity;
- bring index coverage to the complete authorized corpus;
- remove newest-N as a semantic-recall boundary;
- use a resident matrix or ANN index;
- embed pending Raw passages;
- use stable rank fusion;
- enable and measure the reranker;
- return `index_incomplete` rather than a confident absence when coverage is partial.

### P4: bring message history to parity

- build conversation passages;
- include adjacent context;
- add temporal filters;
- add typo and keyboard-layout repair;
- add embeddings;
- add a reranker;
- add conversation, person, channel, and role filters;
- expose the unified `archive_search` contract.

### P5: use the new hardware for backfill and V12

After the schema and contracts stabilize:

- recompute semantic titles;
- extract keyphrases;
- classify dates by role;
- build typed facets;
- rebuild passages;
- store extractor and model revisions;
- compare retrieval against the baseline;
- then enable V12 planning over the new contour.

Backfill must be:

```text
resumable
idempotent
versioned
rebuildable
audited
```

It must never convert a pending source into canonical knowledge merely because a large model assigned high confidence.

## Release criteria

Recommended gates:

- 100% of authorized, live, text-bearing files have a catalog entry and passages, or an explicit `index_incomplete` reason.
- Pending files are semantically discoverable by their authorized owner but are never represented as confirmed knowledge.
- Exact, partial, alias, and typo filename navigation pass the release battery.
- No date role is silently substituted for another.
- Friday never says "this is not in the archive" when any relevant index is partial, stale, failed, or capped.
- Candidate recall@50 on the real difficult-query set is at least 0.95.
- Message-history results include enough adjacent context to be meaningful.
- Changing the embedding model makes old vectors explicitly incompatible and starts a backfill.
- Physical registration, catalog coverage, passage coverage, and semantic coverage are diagnosed separately.
- Every returned factual excerpt is reauthorized against its source identity and lifecycle state.

## Immediate recommendation

Do not begin with MCP, V12 expansion, or a 120B deployment as the main fix.

Begin with this vertical slice:

1. A rebuildable `DocumentCatalog` over Raw, Inbox, and Knowledge Objects.
2. Semantic passages for all authorized documents, independent of promotion.
3. Separate `filename_original` and `semantic_title`.
4. Typed temporal facts and explicit date roles.
5. One `archive_search` contract spanning documents, knowledge, and messages.
6. Full-corpus embeddings and a dedicated reranker.
7. V12 planning only after these foundations are measurable.

That slice changes Friday from a system that can read a file when precisely pointed at it into a system that can genuinely remember what it was given.

The current problem is not that Friday lacks intelligence. It is that three librarians maintain three incompatible catalogs. More hardware makes the librarians smarter. The first architectural requirement is still one catalog.

## Repository files reviewed

- [`docs/DOCUMENT_CONTOUR_RELEASE_CRITERIA.md`](../docs/DOCUMENT_CONTOUR_RELEASE_CRITERIA.md)
- [`docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md`](../docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md)
- [`docs/NEW_ROUTER_MODEL_MIGRATION_PLAN.md`](../docs/NEW_ROUTER_MODEL_MIGRATION_PLAN.md)
- [`.env.example`](../.env.example)
- [`friday/documents/`](../friday/documents/)
- [`friday/ingestion/_files.py`](../friday/ingestion/_files.py)
- [`friday/ingestion/_advice.py`](../friday/ingestion/_advice.py)
- [`friday/ingestion/_base.py`](../friday/ingestion/_base.py)
- [`friday/storage/_intake.py`](../friday/storage/_intake.py)
- [`friday/storage/_knowledge.py`](../friday/storage/_knowledge.py)
- [`friday/storage/_conversations.py`](../friday/storage/_conversations.py)
- [`friday/retrieval/`](../friday/retrieval/)
- [`friday/execution_kernel/__init__.py`](../friday/execution_kernel/__init__.py)
- [`friday/time_routing.py`](../friday/time_routing.py)
- [`friday/diagnostics/`](../friday/diagnostics/)
- [`tools/audit_file_registry.py`](../tools/audit_file_registry.py)
- [`tools/retrieval_bench.py`](../tools/retrieval_bench.py)
