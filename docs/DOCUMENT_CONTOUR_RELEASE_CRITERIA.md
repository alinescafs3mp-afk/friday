# Document contour release criteria

This file is the persistent source of truth for the document-focused release
requested on 2026-08-11.  It survives chat compaction and is stricter than a
single bug fix: the release is not complete until every mandatory item below is
implemented, tested and deployed.

## Product principles

- File search, file understanding, navigation between files and grounded
  synthesis are Friday's highest-priority capabilities.
- Give the model the complete relevant authorised context and useful tools.
  Deterministic code enforces real boundaries (tenant/uploader/privacy,
  provenance, completeness, dangerous effects and resource limits); regexes
  and heuristics may guide or accelerate a route, but must not turn unfamiliar
  wording into a false declaration that a capability is unavailable.
- Never silently switch to a different file, person, date role or corpus.  An
  unresolved or ambiguous reference fails closed and asks for clarification.
- Never certify absence, an exact count, a complete set or full-document
  coverage from a capped, partial, OCR-only, failed or unverifiable source.
- Long documents may take longer than short ones, but work must be bounded,
  cancellation-safe and honestly report coverage.  A small fully parsed file
  must not take the expensive long-document route.
- Tests and diagnostics must not write synthetic rows to the production DB,
  expose private document bodies/passwords, or leave temporary files/processes.

## Mandatory behaviour

### Search and navigation

- Resolve current uploads, exact filenames, normalized stems, colloquial names,
  partial filenames and safe typo matches before falling back to body search.
- A deictic reference to the most recently sent file follows upload-message
  chronology, not the immutable Raw Object creation timestamp.
- A Telegram reply to a file resolves that exact file even after byte-level
  deduplication/re-upload; an unresolved pointer never falls back to the newest
  or an older active file.
- A Telegram reply to Friday's text answer restores the exact authorised file
  lineage used by that answer; deleted, ignored, foreign or ambiguous sources
  remain unavailable.
- Search inside already uploaded files uses uploader-scoped lexical, embedding
  and reranker channels.  Canonical Raw bytes and lifecycle/privacy verdicts are
  rechecked before evidence is admitted.  Spreadsheet section headings remain
  connected to their following record.
- Resolve people by exact id/username first, then unique normalized partial,
  layout/transliteration and bounded typo matches (including the unique
  `GBL` -> `JBL` case).  Ambiguity or invisibility fails closed.
- Named-user aggregation selects only that uploader's files.  Wording such as
  "sent/uploaded on 7-11" uses arrival time; explicit "document dated" uses
  document metadata time.  Last-N and all-time scopes expose selected/total and
  every incompleteness ceiling; tenant-wide `collect_files` is not a substitute.

### Reading, analysis and actions

- Small complete Office/ODF documents use one fit-first synthesis path and do
  not emit false partial-material warnings.
- Large documents beyond the context window use ordered, bounded hierarchical
  processing that covers every planned chunk, preserves tail evidence, keeps a
  foreground slot free and cannot mark clipped/failed work complete.
- Images and raster PDFs use vision/OCR as useful advisory evidence, not a
  reason to refuse.  Multi-page scanned PDFs are rendered and processed beyond
  four pages within explicit page/pixel/time budgets; partial OCR remains
  visible and verification stays UNKNOWN where appropriate.
- Ordinary summaries, exact queries, comparisons, counts, cross-file
  aggregation, transformations and exports work from the selected corpus.
  Output files retain source/provenance caveats where evidence was partial.
- Markdown emphasis, lists and quotes survive Telegram rendering without raw
  `*` characters becoming fake separators.

### Metadata and requisites

- "Show/write/list all metadata of this/other/replied document" resolves the
  intended file and returns both sections unless the request explicitly asks
  for only one:
  1. technical/container metadata as stored;
  2. formal requisites from visible content, backed by exact source quotes.
- Technical ODF metadata includes bounded standard fields, document statistics,
  typed user-defined fields, template/reload/link settings and stored signature
  XML facts.  Stored certificate subject/time/ids are never described as a
  cryptographically verified signature; validity remains `not_checked`.
- The same metadata command is format-independent.  For common OOXML, PDF,
  email, EPUB and image files it exposes the bounded metadata actually stored
  by that format (core/application/custom properties, Info/XMP, mail headers,
  OPF or EXIF/dimensions as applicable), with the same total/shown/incomplete
  accounting and without parsing body text merely to print technical fields.
- Content requisites include document title/number, visible issue/registration/
  signing dates, classification/grif, author/sender, addressee, approver,
  signatory with role, organisation and other formal details.
- Container creation/modification dates are labelled as container properties,
  never as the legal/visible document date.  Every cap publishes total/shown
  counts and an explicit incomplete marker.
- Requests about material properties or company details *inside* a document are
  content queries, not metadata-navigation false positives.

### Archives and passwords

- ZIP (ZipCrypto/AES), 7z and RAR are supported within shared member, expanded
  bytes, dictionary, depth, file-count and deadline budgets.  Nested ordinary
  documents larger than the old preview limit are actually processed.
- Encrypted archives ask for a password when absent and distinguish an invalid
  password from an unavailable backend or resource limit.
- Inline/caption and immediate follow-up passwords preserve exact characters,
  including leading/trailing whitespace and Unicode representation.  Only a
  bounded exact-first set of presentation/normalization variants is attempted
  under one shared deadline; this is not brute force.
- Passwords are ephemeral: excluded from messages, Raw Objects, idempotency,
  audit/logs, command argv/environment and durable Telegram queues.  All
  attempted variants are redacted from extracted output.  Backend and bridge
  run with core dumps disabled; Python process memory itself is not claimed to
  be zeroizable.

### Web, MCP and model operation

- Local/private file turns do not leak document/person data to public web tools
  without explicit web intent.  Explicit web requests remain available and
  preserve a bounded source ledger for immediate transforms/exports.
- MCP filesystem access remains code-owned and sandboxed: inbox read/import,
  outbox create-only, no arbitrary server schema or path reaches the model.
- The Qwen reasoning mode must not add hidden `<think>` output or unnecessary
  stages.  Short conversation and small-file paths stay short; long latency is
  attributed to measured stages, not guessed to be paging.
- Embeddings and reranker must be observed in the semantic document-search
  scenario, while final evidence still comes from authorised canonical source.

## Required release battery

Create a separate task-owned, synthetic-only live battery.  It must use an
isolated temporary HOME/SQLite/files/cache/log tree while exercising the real
deployed model, vision, embeddings, reranker and relevant MCP path.  It must not
reuse or overwrite the existing user-owned synthetic-battery files.

Each run contains ten unique end-to-end document scenarios:

1. deduplicated re-upload plus reply-to-file selects the exact ODT over a decoy;
2. reply to an older Friday text answer restores its file, not a newer decoy;
3. approximate/typo filename navigation before body fallback;
4. semantic XLSX lookup across a section heading and record, with embeddings
   and reranker participation and no confident false absence;
5. `GBL` resolves uniquely to `JBL`, then arrival-date/last-N aggregation uses
   only that uploader's files;
6. small complete ODT bare-upload summary is useful, fast-path and not falsely
   partial or replaced by a deed guard;
7. multi-page raster PDF OCR reads pages beyond page four and reports authority
   and coverage honestly;
8. a document larger than the model context answers a tail query and produces
   an ordered full-source summary through hierarchy;
9. an encrypted archive accepts an exact whitespace/Unicode password and its
   nested document becomes searchable without credential persistence;
10. all metadata distinguishes container dates from quoted visible
    number/date/grif/signatory, then a document-derived export/MCP action keeps
    provenance.

The release gate is **two consecutive completely clean runs** of this battery.
Any defect resets the clean-run counter: fix all defects found in that run,
build a new immutable release candidate and restart the count.  Store only
sanitized case IDs, timings, token/tool counters, boolean assertions, hashes and
closed failure codes as evidence.

## Deployment gate

- Immediately before the live-model battery, confirm the Telegram queues are
  empty and stop `friday-bridge` so user traffic cannot contaminate timings or
  source selection.  Keep it stopped through both required clean runs.
- Run only focused unit/integration checks needed for changed contracts, then
  lint/type/compile/diff checks on changed source.
- Obtain an independent frozen-diff review with zero release blockers.
- Commit only task-owned paths; preserve unrelated/user-owned changes and
  artifacts.
- Deploy from an immutable release directory.  Run schema migration through
  normal startup; restart backend first, verify health/MCP/sidecars, then bridge.
  Do not restart the model dispatcher unless the model configuration changed.
- Confirm service health, restart/OOM/error state, empty bridge/outbound queues,
  MCP availability, embeddings/reranker availability and a short model-speed
  smoke after deployment.
