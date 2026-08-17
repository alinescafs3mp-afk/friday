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

Before the first case, the worker must fail closed unless the exact runtime
profile is `qwen36-27b-nvfp4-nvidia` and its independent
`document_map_max_concurrency` is exactly `1`.  Model-generation evidence is
content-free and comes from runtime call boundaries, not prompt inspection:
direct attachment synthesis, hierarchy plan, MAP leaf, REDUCE, hierarchy-final
synthesis and verifier.  Every model call records started/completed/failed/
cancelled; MAP additionally records the plan cardinality, current active calls
and peak active calls.  A missing counter or missing hierarchy cardinality is a
failed case, never an inferred zero.

The release generation budgets for the two routing canaries are exact:

| Case | Direct | Hierarchy | MAP | REDUCE | Final | Verifier | Total model calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D06 small fit-first | 1 | 0 | 0 | 0 | 0 | 0 | 1 |
| D08 larger than context | 0 | 1 complete plan | `planned = started = completed > 0`, peak active `1` | 0 | 1 | 1 | `MAP planned + 2` |

For both cases every failed, cancelled and unclassified model generation is
exactly zero, all started calls complete, and no call beyond the declared
budget is allowed.  D08 uses a non-repetitive synthetic source so the exact-RLE
fast path cannot replace the required MAP proof.  A verifier rejection followed
by repair/re-verification is therefore a red run even if the eventual prose is
acceptable: the immutable candidate did not meet the declared clean-call
envelope.

Every live battery execution must be fresh, not a replay of canned bytes.  The
controller generates a new unpredictable run id; both runs and all ten cases
derive distinct filenames, source refs, document facts, control markers and
decoys from it.  Natural prompts rotate through bounded contract-equivalent
wordings, so the second clean run and later retries do not merely repeat the
same phrase.  Oracles remain deterministic and validate the underlying
contract, not a memorised marker or one exact wording.  No case may reuse a
prior run's database, files, MCP outbox, conversation or model-prefix cache key.

The release gate is **two consecutive completely clean runs** of this battery.
Any defect resets the clean-run counter: fix all defects found in that run,
build a new immutable release candidate and restart the count.  Store only
sanitized case IDs, timings, token/tool counters, boolean assertions, hashes and
closed failure codes as evidence.

The complete D01--D10 contract matrix belongs to the zero-skip offline gate.
The live controller reuses that immutable evidence and runs only the three
nondeterministic end-to-end canaries D06 (direct document synthesis), D07
(multipage vision) and D08 (hierarchical MAP synthesis), twice in one
invocation.  This avoids spending remote generations on deterministic storage,
archive and export contracts already proved against the same commit.  D06 and
D08 remain routing canaries inside each three-case run, not separate CLI
targets.  Each worker owns a distinct
POSIX process group.  After run 1 the controller must reap the worker and prove
that the entire group disappeared without TERM/KILL cleanup.  A surviving
descendant, timeout, non-zero worker exit, lifecycle/MCP close exception, MCP
cleanup-timeout warning or stranded-shutdown warning makes the run red even if
all product assertions passed.  The controller atomically persists and rereads
an owner-only sanitized `run-1-receipt.json` with `teardown_clear=true` before it
may create worker 2; run 2 gets the same post-exit receipt.

The controller converts SIGINT/SIGTERM into a fail-closed unwind.  On POSIX it
atomically blocks the complete INT+TERM set before `Popen`, keeps it blocked
until both the process handle and exact PGID are bound inside the outer cleanup
contour, and then restores the parent mask.  Because that mask is inherited
across exec, the hidden worker unblocks the same set as its first action before
any database, MCP or model setup; a TERM pending since spawn is therefore
delivered rather than silently left blocked.  From spawn through the first
post-`communicate` group audit and final audit, every `BaseException` enters the
same bounded cleanup: TERM the group, wait, KILL the surviving group regardless
of leader state, reap the leader, and finally prove the group absent.  Any TERM
or KILL cleanup makes the run red even when it succeeds.  INT+TERM stay blocked
as one set throughout worker and controller finalization, so a repeat signal
cannot interrupt that sequence; the first controller signal still determines
the truthful `128 + signal` exit.  A false/exceptional final audit is never
discarded and worker 2 is never created.  SIGKILL and power loss remain an
operator recovery case; they are not falsely described as catchable cleanup.

There is an observable fail-closed barrier between those workers.  The operator
pre-creates an empty non-symlink `0700` barrier directory and passes it through
`--inter-run-barrier-dir`.  After the run-1 receipt, the controller writes a
`0600`, single-link observer request bound to the immutable commit, run index,
run-id hash, exact worker-report hash and an unpredictable private challenge.
It then waits boundedly for an atomically published `0600` response proving all
of the following immediately before worker 2: bridge still stopped and its
exact operator `ProcessLease` guard still held; backend healthy with exact
unchanged process identity; durable outbound, inbound and dead-letter counts
all zero; dispatcher identity unchanged.  The response must echo every binding
exactly and contains only
booleans and hashes.  Missing, malformed, stale, substituted, false or timed-out
observer evidence stops the streak before worker 2.  Neither request nor
response may contain service environment, messages, prompts, model bodies,
tokens, document facts or raw diagnostics.  The barrier directory is
single-invocation evidence: after any request, receipt, interruption or
uncertainty it is consumed.  A retry must use a newly created empty private
directory; stale files are a hard stop, never resume evidence.

### One-shot operational helper

The only supported way to run the canonical two-run release battery during the
bridge outage is `tools/document_contour_release_operator.py`.  Invoke it once
from the immutable clean candidate, with explicit unit names and absolute
paths; do not invoke `document_contour_live_battery.py` separately:

```console
python -B tools/document_contour_release_operator.py \
  --run-live \
  --freeze-commit <exact-40-hex-HEAD> \
  --env-file /absolute/path/to/owner-only.env \
  --inter-run-barrier-dir /absolute/private-parent/empty-barrier \
  --backend-unit <exact-backend.service> \
  --bridge-unit <exact-bridge.service> \
  --report /absolute/private-parent/new-operator-report.json
```

The env file is a single-link owner `0600` regular file and remains pinned by
descriptor and content hash.  The empty barrier and its dedicated, quiescent
parent are owner `0700` directories.  Both parent and barrier are pinned by
directory descriptors; their lexical identities are rechecked through the
controller exit.  The helper derives the runner's complete model-setting
allowlist from those pinned bytes and passes only the values in the private
child environment; neither credentials nor the env-file path enter the child
argv.  The internal `--operator-model-env-only` runner mode requires every
allowlisted key to be inherited, starts from an empty source mapping and never
resolves or reads `FRIDAY_ENV_FILE`, `--source-env-file` or repository
`.env.local`; a missing key or conflicting source-file option is a hard red.
Every request, receipt and response is read relative to the held descriptor
with no symlink following, single-link `0600` validation, canonical JSON
validation and stable before/open/after identity.  Publication is create-only
via `renameat2(RENAME_NOREPLACE)`.  The optional report path must not exist,
must be outside the barrier and env file, and is likewise published create-only
in an owner `0700` parent.  A consumed barrier or report path is never reused.

The helper has one fail-closed state sequence:

1. While the bridge is active, pin the backend PID with a pidfd and exact
   systemd fingerprint, bind the active bridge service PID to its exact recorded
   lease protocol, require backend health and zero physical outbound, and hash
   the dispatcher's sole positive finite `process_start_time_seconds` sample.
2. Arm restoration before issuing the single bridge stop.  Require the unit to
   become exactly inactive/dead with zero main/control PIDs, the old pidfd exited
   and its cgroup recursively unpopulated.
3. Read the stopped zero-queue backend snapshot, acquire and retain the exact
   bridge `ProcessLease`, then use only the public descriptor-bound
   `collect_document_contour_guarded_bridge_queue_snapshot` projection for
   inbound/dead-letter counts.  The main DB physical-outbound projection remains
   the authenticated backend HTTP snapshot, whose bridge queue is intentionally
   `active_uninspected` while the operator guard is held.
4. Launch exactly one canonical battery controller through absolute
   `/usr/bin/systemd-run` in one transient user scope, with
   `KillMode=control-group` and `--expand-environment=no`.  Before owner handoff,
   require the exact scope to be loaded, active/running, retain that kill mode,
   have a live controller pidfd and have a recursively populated cgroup.  Bind
   its controller handle while INT+TERM are blocked; the controller explicitly
   unblocks the inherited mask.  Validate the exact run-1 receipt/request,
   repeat the backend, bridge, guarded queue and dispatcher attestations, then
   publish the exact
   `friday.document-contour-live-battery.observer-response.v2` response with
   `bridge_operator_guard_held=true`.  The obsolete claim
   `bridge_lease_free=true` is invalid.
5. Keep the guard through worker 2, controller exit, whole-scope emptiness, both
   receipts, the canonical battery report and the final repeated attestations.
   Only then may cleanup release the guard and make the restoration attempt.

After the stop contour is armed, normal completion, `Exception`, any catchable
`BaseException`, SIGINT and SIGTERM all enter the same finalizer.  It first
terminates a surviving transient scope with one bounded TERM wait and one KILL
of all cgroup members plus one controller-PGID KILL if either the scope remains
populated or the synchronous controller cannot be reaped after TERM.  After
that single escalation it performs a second bounded controller wait and a final
cgroup-plus-pidfd audit; an empty cgroup alone never hides an unreaped leader.
It then releases the guard and makes exactly one bounded, non-retried bridge
start request, confirming the new service PID owns the exact bridge lease.
Cleanup or start uncertainty, or a changed backend fingerprint after
restoration, makes the run red but cannot schedule another start.  SIGKILL,
host loss and user-systemd loss remain explicit manual recovery cases.

The backend owner token exists only in the local process and authenticated HTTP
header.  Backend and dispatcher clients reject redirects and ambient proxies;
TLS verification is never disabled when HTTPS is configured, CA variables are
preserved, and every streamed body has both a byte ceiling and a total monotonic
deadline in addition to per-operation timeouts.  Systemd and Git calls use the
absolute `/usr/bin/systemctl`, `/usr/bin/systemd-run` and `/usr/bin/git`
binaries.  They receive only the validated owner runtime directory and a narrow
process environment; ambient `PATH`, `LD_LIBRARY_PATH`, proxy, remote-bus and
Git-repository controls do not cross the boundary.  Child stdout/stderr use
unnamed files; stdout above the fixed limit is rejected and stderr is never
published.  Public stdout and the optional report contain only
the commit hash, booleans, timings, evidence hashes, signal name and closed
failure codes—never PIDs, paths, URLs, epochs, challenges, tokens or bodies.

This helper is supported only on Linux with pidfds, cgroup v2 `cgroup.events`, a
working user systemd manager supporting transient scopes and
`KillMode=control-group`, and `renameat2(RENAME_NOREPLACE)`.  A missing interface
is a hard red result (pre-stop where detectable), not permission to fall back
to controller-PGID cleanup or overwrite publication.  Its offline contract must
pass both serial and `-n 12` execution of
`tests/test_document_contour_release_operator.py` and
`tests/test_document_contour_live_battery.py`, plus
Ruff, formatting, mypy, byte compilation and `git diff --check`; these tests do
not contact live HTTP, model or service interfaces.

### Test execution budget

- The working cadence is product-first: run the live product battery, collect
  its defects, fix that batch, run only the necessary regression/static checks,
  freeze a new candidate and return to the live product battery.  Offline test
  volume is never a substitute for exercising the product again.
- Do not turn each small edit into a broad or multi-hour test campaign.  After
  a narrow fix, run only its exact reproducer, the nearest two or three positive/
  negative controls, and static checks for the changed files.
- Run the broader affected offline suite once, only after the complete fix set
  is frozen and immediately before committing the release candidate.  Do not
  repeat it after every regex, guard or fixture adjustment.
- Live inference is reserved for release gates: a specifically justified smoke
  when needed, followed by the required two full batteries.  Never run live
  cases concurrently, and never repeat a red live case before its cause is
  diagnosed and a new immutable candidate exists.
- Prefer adding one precise regression that proves the discovered contract over
  growing an unbounded permutation matrix.  Extra fuzzing is justified only by
  a concrete unresolved security or correctness boundary.

## Deployment gate

- Immediately before the live-model battery, confirm the Telegram queues are
  empty and stop `friday-bridge` so user traffic cannot contaminate timings or
  source selection.  Keep it stopped through both required clean runs.  The
  same queue/backend/dispatcher invariants are re-attested at the mandatory
  inter-run barrier; the controller never starts the second worker on an
  uncertain teardown or observer result.
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
