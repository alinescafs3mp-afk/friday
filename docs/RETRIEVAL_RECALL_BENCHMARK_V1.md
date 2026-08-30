# Retrieval Recall Benchmark V1

This package is offline evaluation infrastructure around Friday's shipped
`archive_search` path. It does not alter routing, retrieval, ranking, storage,
schema, model behavior, publication, or production activation.

## What is measured

The code-owned manifest has 21 synthetic cases, at least two in each closed
class:

1. `approximate_content`
2. `approximate_date`
3. `old_file`
4. `pending_file`
5. `unhelpful_filename`
6. `typo_layout`
7. `person_topic`
8. `topic_month`
9. `message_paraphrase`
10. `unknown_corpus`

Cases reuse `ArchiveSearchRequest` as the only query/filter contract. A qrel
contains an `ArchiveSearchCorpus`, a keyed opaque `SourceRef` identity, a
body-free keyed digest of the exact validated `PassageRef` (including revision
and exact span/window identity), relevance grade 1–3, and an optional
`TemporalRole`.
No-hit cases are explicit and cannot also contain positive qrels.

The report contains these fixed-point integer metrics (parts per million):

- candidate recall@50 and recall@100, micro-averaged over unique qrels;
- MRR@10 and nDCG@10, macro-averaged over expected-hit cases;
- false-absence rate for expected-hit cases;
- typed date-role accuracy for matched temporal qrels;
- catalog, passage and embedding coverage when their denominators were observed;
- the same fixed catalog per taxonomy class and archive corpus.

`grounded_answer_accuracy` is always typed `not_measured`. The citation-only
carrier used to obtain the shipped phase-2 projection is not a generated answer.

One candidate can credit a qrel only once. Qrel source identities are unique;
duplicate candidates do not increase recall. Rank comes from the shipped public
citation label (`A1` … `A100`), never from projection iteration order. All
arithmetic is deterministic integer arithmetic; JSON contains no floats.

## Coverage and absence

The benchmark projects a validated `SearchCoverage` after removing its
process-private execution binding. Absence is derived, not trusted. An empty
result is `authorized_absence_confirmed` only when every canonical requested
target is exactly `complete`, has a known eligible count equal to the examined
count, has zero matches and returns, has no cursor, and is current and
reauthorized.

Any incomplete, stale, unavailable, permission-filtered, backfill-pending,
embedding-incompatible, or capped target yields `not_established`. Candidate
evidence yields `evidence_found`. In particular, today's unsupported lanes for
documents, messages, and external sources never become a dishonest “not found”.
Coverage metrics are separate from that safety oracle: every lane with an
observed denominator contributes its exact `examined / eligible_authorized`
ratio, including partial or capped work. A known empty lane contributes full
observed coverage. A lane whose denominator was not observed keeps the metric
typed `unavailable` rather than inventing a value. Per-corpus coverage includes
only that corpus's retrieval lanes, including inside a mixed-corpus request.

## Real ephemeral path

`run-ephemeral` creates a private temporary Friday home, disables every model,
worker, connector, embedding, Obsidian, and code-execution feature, initializes
a fresh Friday SQLite store, and seeds only code-owned synthetic records. It
never accepts a database or home path. Same-process runs are serialized while
the isolated environment is active, so no concurrent caller can observe or
restore another run's temporary Friday variables.

Each case executes this existing path:

1. `create_archive_model_batch_ledger`;
2. `prepare_archive_search_in_transaction` inside a caller-owned transaction;
3. exact `AuthorizedArchiveBatch.model_visible_canonical_bytes` admission;
4. real continuation while available, bounded at rank 100;
5. `freeze_for_publication` and
   `refresh_archive_search_reauthorization_in_transaction`;
6. a code-owned citation-only carrier passed to
   `attest_archive_search_before_publication`;
7. immediate body-free projection from
   `ArchiveSearchAcceptedCandidateProjection.candidates`.

Every prepared page must also pass `PreparedArchiveSearch.attests_origin` for
the exact private request identity and caller-supplied release discriminator.
The predicate reconstructs the sealed accepted-turn snapshot internally, so
message and mixed-corpus pages remain bound even though the service transforms
their raw discriminator around the accepted conversation boundary. It returns
only a boolean and exposes none of that boundary material.

The transient public page is used only to reconstruct typed coverage and retain
`TemporalRole` enums. Query, title, filename, path, excerpt, public randomized
handles, continuation tokens, execution bindings, tool payloads, and logs are
discarded. The temporary directory is closed and deleted before the report is
returned. `release_sha256` binds the observation to a stable snapshot of every
local Python source under `friday/`, including the consumed retrieval, storage,
permission, configuration, and evaluator surfaces; `evidence_source`
separately says `synthetic_ephemeral` or `owner_private_jsonl`. Neither field is
runtime authority.

Only the in-process phase-2 projection factory can mint a scoreable
`synthetic_ephemeral` observation. Serialization deliberately drops that
process provenance, so a fixture cannot round-trip arbitrary candidates and
then reclaim shipped-evidence status through `score`. `run-ephemeral` performs
search and scoring atomically. Owner-supplied cases and observations use the
explicit `owner_private_jsonl` label.

## Canonical contracts

`RecallCaseV1`, `RecallObservationV1`, and `RecallReportV1` are immutable,
closed, bounded contracts. Parsers require ASCII canonical JSON with sorted
keys and no insignificant whitespace. They reject unknown or duplicate keys,
duplicate identities, non-finite or floating numbers, bool-as-int, control or
surrogate text, noncanonical ordering, oversized values, and forged digests.
Case, observation, and report manifests use domain-separated SHA-256.

Each private case carries a distinct 32-byte `privacy_key_hex`. It keys source,
passage, case-label, and private-case commitments; it is present only in the
private case input. Generate a fresh cryptographically random value for each
owner case. Consequently observations and reports cannot correlate a raw case
label or low-entropy `SourceRef` across cases. The synthetic manifest uses
code-owned keys only for code-owned identities so its output stays deterministic.

Reports retain only opaque case IDs with case/observation digests, aggregate
metrics, and aggregate taxonomy/corpus breakdowns. In-memory scoring can expose
typed per-case diagnostics to the ephemeral harness and focused tests, but
those case-mapped outcome, rank, grade, temporal, count, and coverage-vector
facts are deliberately excluded from report JSON. Report factories derive
aggregates from the scored facts before applying this privacy projection.
Coverage cells carry only aggregate target/unknown counts and fixed-point score
sums; the report binds its taxonomy totals, expected-corpus totals, and a typed
off-expected-corpus residual so mixed requests cannot make the views
contradictory. A separate unmapped coverage-configuration histogram records the
taxonomy and expected corpus, total lane target/unknown/score vectors, expected-corpus
unknown/score vectors (with its targets derived from the shipped lane map), and
an oracle-derived absence-readiness flag, plus case and false-absence counts. It
derives both expected and off-expected
coverage totals exactly and requires every configuration
containing a confirmed absence to have full known coverage in every requested
lane and explicit oracle readiness. Thus a stale or partial lane whose numeric
coverage ratio happens to be full still cannot authorize absence. No case or
source mapping is exposed.

Non-coverage cells carry aggregate sufficient facts: bounded case/qrel and
recall counts, a ten-bin first-relevant-rank histogram, and an unmapped
histogram of complete metric configurations. Each configuration includes
expected grade and typed temporal-grade counts, top-10 grades and temporal
correctness, rank-11–50 and rank-51–100 grade/temporal match counts, a typed
absence decision, the same typed coverage configuration, and a case count. The
joint configuration prevents an absence decision from borrowing complete
coverage owned by a different aggregate bucket. These facts exactly derive
recall, reciprocal-rank, nDCG, false-absence, and dated-role totals. Those facts
deterministically derive the published metrics and must sum exactly across both
taxonomy and corpus partitions. They contain no case mapping. Closed aggregate
validation ties all facts and the exact archive lane-count relationships to the
report's bounded case count. Reports contain no query, prompt, title,
filename/path, excerpt/body,
actor/person/conversation text, tool arguments, tool output, or logs.
Owner-private JSONL is read only from an explicit CLI argument, within a 16
MiB/1,000-record bound, and is never copied into a report.
Private case inputs must be an owner-held, single-link regular file with no
group/world permissions. Sidecar parents must likewise be owner-held lexical
directories without group/world write permission. All requested destinations
are preflighted before publication, and sidecars are published at mode `0600`
without overwriting or following a link. A caught publication failure triggers
inode-bound rollback of the bounded group. This is not a cross-directory or
process-crash atomic transaction.

## CLI

```console
python -m friday.retrieval_benchmark validate cases CASES.jsonl
python -m friday.retrieval_benchmark validate observations OBSERVATIONS.jsonl
python -m friday.retrieval_benchmark validate report REPORT.json
python -m friday.retrieval_benchmark run-ephemeral
python -m friday.retrieval_benchmark run-ephemeral --cases-out CASES.jsonl --observations-out OBS.jsonl
python -m friday.retrieval_benchmark run-conversation-ephemeral
python -m friday.retrieval_benchmark score CASES.jsonl OBSERVATIONS.jsonl
python -m friday.retrieval_benchmark compare BASELINE.json CANDIDATE.json
```

Successful output is one canonical JSON record. `validate` checks canonical
shape and integrity, not remote provenance. Exit 2 means a closed input or
output contract was rejected, exit 3 means the genuine ephemeral archive path
failed, and exit 4 means `compare` detected a metric regression or the
conversation matrix retained a measured gap. Sidecars use
exclusive creation and never overwrite an owner file. At most two sidecars may
be requested by one invocation, each at most 16 MiB and together at most
32 MiB.

Comparison requires the same case manifest and evidence source and fail-closes
if case-expectation or coverage-plan signatures drift. It compares available
ratios exactly by integer cross-products, including changes smaller than one
display ppm. Each regression row carries the exact before/after numerator and
denominator alongside display ppm. It can report a regression in aggregate,
taxonomy, or corpus scope; loss of an available metric is itself a regression. Its
`release_threshold` is always `not_assessed`; this benchmark does not declare
the S4 release target met.

## R1 corpus recall gap closure

The R0 baseline measured candidate recall@50/@100, MRR@10 and nDCG@10 at
`14/20` (`700000` ppm), with false absence `0/20` and date-role accuracy
`6/6`. Body-private diagnosis assigned every non-hit to one closed class; the
report and this record retain only the opaque case identity:

| Opaque case ID | Classification | R1 disposition |
| --- | --- | --- |
| `3abe53303c2f06b4cf0a7e3e7300669ed46bf0e00ed40ed6409a06e43c08c950` | budget cap | fixed by deterministic ranking inside the existing cap |
| `82a4cf00058a0c8c7554253c96777ecd121048b65fadd06676721cc0e7345898` | accepted projection | intentionally remains pending/noncanonical |
| `d906592d6ca4559b25ac055d299a985e91326787e90525f7bdc49ae8a6c76e37` | accepted projection | intentionally remains pending/noncanonical |
| `2e7ff16ab3b3476ec547ef96a04755a0ccb8e8b90e0e987a606f6f5887cb0237` | intentionally unsupported | document upload time has no attested storage column |
| `9e1ff2af99b314db94c4a5666e29c401f951a9ebe6a39834726d774870818c86` | intentionally unsupported | document upload time has no attested storage column |
| `1ab469645a5041da1200ec9a1e5a296ee6669f7a3b10317076c9ea4cc06c2090` | intentionally unsupported | document upload time has no attested storage column |
| `e131e3d818fb395177155a1faa1de2083e34ad8561f1517dc8b9a9b506a6a17b` | corpus incompleteness | expected no-hit remains `not_established` |

The repaired document had lexical lane rank 26 among 26 body matches and was
therefore lost before federation by the existing 20-result lane cap. R1 does
not raise that cap or add a cursor. For the documents lexical lane only, a safe
filename containing every existing nonempty canonical lexical term receives a
deterministic ordering boost. Eligibility, matched count, exact body score and
passage evidence remain body-derived; malformed metadata contributes no boost.
The exact body score, time and source identity remain the subsequent ordering
components. The measured lane remains honestly `CAPPED/PARTIAL`, with 26
matches and 20 returned candidates.

The resulting benchmark is `15/20` (`750000` ppm) for candidate recall@50 and
@100, MRR@10 and nDCG@10. False absence remains `0/20`; date-role accuracy
remains `6/6`. The two pending cases are not promoted to factual authority, the
three unsupported upload-time cases remain unavailable rather than aliased to
receipt time, and incomplete no-hit coverage remains uncertain.

## R5 conversation journey matrix

R5 is a separate 24-case manifest and does not modify the frozen 21-case R0/R1
corpus or its taxonomy. It reuses `RecallCaseV1`, `RecallObservationV1`, the
existing scorer, and the existing body-free `RecallReportV1`. The matrix has
four positive cases in each of six closed cells:

1. archive search controls;
2. lexical/`MESSAGE_HISTORY` fallback;
3. exact adjacent-message windows;
4. cross-lane and continuation diversity;
5. clean-restart selected-evidence replay;
6. owner, accepted-boundary, role, and lifecycle privacy.

The corpus is seeded in explicit phases. Foreign lexical postings are
converged first, then the owner writer commits one anchor, storage is closed and
reopened, and the returned private cursor advances a different projection while
an intentionally reset earlier prefix remains untouched; that proves the
opaque cursor was honored before the owner projection converges. Only afterward
are backfill-pending rows, an appended tail, an actual `created_at` source
reset, the accepted boundary, and an excluded post-boundary row inserted. No
writer runs after those late phases. This measures the shipped fallback semantics without
fabricating a second index or treating the partial lexical derivative as
absence authority.

Each case uses the same preparation, model-byte admission, continuation,
reauthorization, publication attestation, and body-free observation path as the
main ephemeral benchmark. Four selected conversation sources are then reduced
to canonical `ArchiveSearchSelectedEvidence`, serialized privately, reparsed
after a second clean storage reopen, and replayed through
`replay_archive_evidence_in_transaction`. Only exact status and a digest of the
model-visible replay bytes remain in the measurement facts.
The one long pre-R5 replay expectation is bound to frozen snapshot and
model-byte digests captured from the released head/tail renderer; the benchmark
does not derive its compatibility oracle from the renderer under test.

Privacy measurements reject more than foreign ownership: fixed forbidden
message/source identities make accepted-boundary, same-principal role, and
lifecycle decoys observable as closed gaps even when the expected positive qrel
is also present.

The reproduced retrieval defect was a long adjacent window whose head/tail
excerpt truncation removed the short matched row in the middle. The bounded
renderer now retains a complete matched row whenever that row itself fits the
1,900-character limit, while allocating remaining space to the nearest context
on both sides. Replay uses the same renderer for new selections and retries the
released V1 head/tail rendering only when its durable snapshot digest matches,
so pre-R5 long-window selections do not become false drift after upgrade.

`run-conversation-ephemeral` emits only the existing canonical body-free report.
It returns exit 4 when any closed adjunct measurement reports a retrieval,
window, authority, channel, or replay gap; no private diagnostic or sidecar is
written by that command.

The fixed corpus measures candidate recall@50 at `23/24` (`958333` ppm) and
candidate recall@100 at `24/24` (`1000000` ppm), with false absence `0/24`.
The sole rank beyond 50 is the deliberate 25-source continuation contour; its
exact qrel is recovered at citation rank 64 through the real continuation and
is not classified as a lost source. All 24 exact source/window qrels are hits,
all four restart replays are exact, and the closed adjunct gap count is zero.

## Known limits

- The shipped accepted candidate projection deliberately excludes pending or
  otherwise noncanonical and navigation-only candidates. Pending-file misses
  are therefore visible in baseline recall, while their randomized public
  handles are never promoted into fake stable identities.
- One archive request page is limited to 20 results. The harness follows real
  continuation, but the shipped 32,768-byte model-admission ledger may stop a
  rich result set before rank 100; it is never bypassed with an alternate
  retriever. The stock corpus includes a deterministic crowded query that
  exercises continuation and exposes any candidate loss at that boundary.
- Embedding coverage is typed unavailable when the disabled ephemeral path
  cannot observe an eligible embedding denominator.
- Canonical digests detect mutation and bind exact inputs; they are not a
  remote signature. Shipped-evidence provenance is intentionally process-local,
  because V1 adds no key service, durable store, or schema.
- Conversation lexical selection still uses a fixed global candidate pool
  before owner filtering. Foreign-first saturation can therefore remove the
  owner's lexical channel. R5 records that lane as partial and requires the
  independently authorized complete `MESSAGE_HISTORY` lane to preserve owner
  recall; it does not promote lexical zero to durable absence.
- The conversation matrix drives the shipped archive preparation/publication
  facade directly. It does not claim coverage of the outer model tool-parser or
  capability router, and it performs no production activation.
