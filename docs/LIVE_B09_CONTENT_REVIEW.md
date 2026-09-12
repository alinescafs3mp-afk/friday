# Independent content evidence (B09 and the unbound B03 question)

The official A+B pair has five B09 answers and the unbound B03-15 answer whose
meaning is accepted only by an
independent visible TUI reviewer. The runner still decides every HTTP, structure,
length, state, privacy, transport and lifecycle condition. A review can add the
missing meaning proof; it cannot clear any closed failure.

This flow applies only to suite revision
`r10-astra-harness-086-independent-content`. Historical pair artifacts retain
their original interpretation and bytes. The ordered open set is `SYN-B03-15`,
`SYN-B09-02`, `04`, `06`, `08` and `10`; A09 and all 39 source-bound A/B03 questions
keep their existing code-owned exact oracles.

B03-15 asks what an opaque product path must return. A correct bare CSV count does
not answer that method obligation. The review must require a useful explanation
of the unknown contract or an informative conditional answer. CSV facts are only
additional/conditional context. The signed plan includes the exact fixture hash,
filename, all independently parsed cells, header and counts before any answer is
observed. The sealed task carries these same facts; they check concrete claims but
do not impose a universal method result. HTTP, privacy, state, effects, transport
and lifecycle remain mandatory. This one case requires a real model response with
no model failure, not a fabricated `office_exact_owned` assertion.

## Boundaries

- Keep the HMAC key outside the repository, the Friday home, run directories,
  environment variables and review payloads. It must be an owned regular `0600`
  file with at least 32 random bytes.
- Keep plans, lab delivery records, review results, per-case receipts,
  acceptances and final aggregates private (`0600`, parent directories `0700`).
- Predeclare the run ID, candidate, manifests, rubric, six question hashes and the independent B03 source facts,
  independent reviewer identity, reserved lab job ID, physical pair directory and
  logical TASK/RESULT event IDs before A starts. A different physical run requires
  a new plan and a new job; existing run directories are never reused.
- The reviewer must be a different TUI thread and generation from the owner,
  author and implementer. Friday does not grade itself.
- Never publish raw questions, responses, rationales, receipts or the key.

## Root flow

Set a private operator directory and create the key once. Do not export its path or
contents:

```bash
umask 077
review_root="$(mktemp -d -p /var/tmp friday-b09-review.XXXXXXXX)"
head -c 32 /dev/urandom > /secure/operator/path/friday-b09-root.key
chmod 600 /secure/operator/path/friday-b09-root.key
```

Before the run, reserve one independent reviewer assignment, a fresh lab `job_`
ID (32 lowercase hex digits), and logical TASK/RESULT event IDs. Select the existing
operator-owned lab store. The plan captures its device/inode and attached TUI
instance/session/epoch. Reviewer generation is that instance ID; reviewer thread
is that session ID. This is a root trust decision, never a path from a reviewer
response. Create the signed plan with those real identities:

```bash
pair_root="$review_root/pair"
.venv/bin/python -B tools/synthetic_live_b09_evidence.py plan \
  --lab-root /home/jericho/jericho/.git/friday-lab/v2 \
  --lab-job-id RESERVED_JOB_ID --pair-directory "$pair_root" \
  --key-file /secure/operator/path/friday-b09-root.key \
  --output "$review_root/plan.json" \
  --reviewer-id REVIEWER_ID --reviewer-generation REVIEWER_GENERATION \
  --reviewer-thread REVIEWER_THREAD \
  --owner-generation OWNER_GENERATION --owner-thread OWNER_THREAD \
  --authoring-session-id AUTHORING_SESSION \
  --implementer-session-id IMPLEMENTER_SESSION \
  --assignment-id ASSIGNMENT_ID --assignment-generation 1 \
  --task-event-id TASK_EVENT_ID --result-event-id RESULT_EVENT_ID
```

Run the new official pair from one candidate snapshot:

```bash
pair_root="$review_root/pair"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py \
  --env-file "$FRIDAY_ENV_FILE" --both --concurrency 4 \
  --run-directory "$pair_root" \
  --b09-review-plan "$review_root/plan.json" \
  --root-review-key /secure/operator/path/friday-b09-root.key
```

The initial command deliberately exits `4`: its closed A/B rows are evidence, but
the pair is not complete before independent content receipts exist. Build the sealed
review TASK from both the B03 and B09 raw evidence:

```bash
.venv/bin/python -B tools/synthetic_live_b09_evidence.py task \
  --key-file /secure/operator/path/friday-b09-root.key \
  --plan "$review_root/plan.json" \
  --pair-report "$pair_root/pair-aggregate.json" \
  --b03-evidence "$pair_root/battery-b/pass-03/evidence/raw-responses.jsonl" \
  --b09-evidence "$pair_root/battery-b/pass-09/evidence/raw-responses.jsonl" \
  --output "$review_root/review-task.json"
```

Submit the sealed task to the existing visible Grok lab with the reserved job ID,
`kind=source-review`, `identity=owner`, `execution_mode=ATTACHED_TUI_MONITOR`, and
`goal` equal to the review assignment. Its RUN payload must include a
`content_review` object containing:

- `assignment_id`, integer `assignment_generation`, `plan_sha256` (canonical plan
  JSON, without the file's trailing newline), `task_event_id`, `result_event_id`;
- `task_path` (exactly `$review_root/review-task.json`) and `task_sha256` (file bytes).

The lab applies the sealed rubric independently and writes the declared result
contract to `$review_root/review-result.json`. Publish a normal lab RESULT whose
payload includes `job_id` and the same `content_review` binding extended with
`result_path` and `result_sha256`. Complete the lab's normal ACK_RESULT step. Source
review of the harness is not a content verdict on the actual six responses.

The root issuer reads the preregistered local lab store directly. It requires the
actual RUN, submit receipt, matching job/delivery and TUI identity, execution ACK,
ACK of RUN, completed RESULT, equal retained result payload, and completion ACK.
It rejects cancelled, expired, revised, duplicated or mixed jobs and artifact
hashes. The result file is always the preregistered path; a supplied package path,
reviewer-name string or unsigned ENQUEUED receipt conveys no authority. The signed
per-case receipts retain the observed record hashes for audit.

```bash
.venv/bin/python -B tools/synthetic_live_b09_evidence.py issue \
  --key-file /secure/operator/path/friday-b09-root.key \
  --plan "$review_root/plan.json" \
  --task "$review_root/review-task.json" \
  --result "$review_root/review-result.json" \
  --output-directory "$review_root/receipts"
```

The local operator and its preregistered lab store are trusted. This does not claim
protection against an operator or a hostile process with the same OS account
rewriting that store or accessing the root key. The application receives neither.
Synthetic store fixtures test validation logic; only an actual delivered review
can supply live content acceptance.

Bind the exact six receipts, then verify the final aggregate:

```bash
.venv/bin/python -B tools/synthetic_live_b09_evidence.py bind \
  --key-file /secure/operator/path/friday-b09-root.key \
  --plan "$review_root/plan.json" \
  --pair-report "$pair_root/pair-aggregate.json" \
  --receipt-directory "$review_root/receipts" \
  --acceptance-output "$review_root/content-acceptance.json" \
  --output "$review_root/final-pair.json"

.venv/bin/python -B tools/synthetic_live_b09_evidence.py verify \
  --key-file /secure/operator/path/friday-b09-root.key \
  --final-pair "$review_root/final-pair.json"
```

Only `verify` exit zero and `final-pair.json` with signed `pair_clean: true` satisfy
the A+B consumer. Missing, failed, mixed, replayed or modified evidence exits nonzero.
The initial `pair-aggregate.json`, either battery aggregate, and all closed-only rows
remain non-certifying.
