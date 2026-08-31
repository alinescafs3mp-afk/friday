# Synthetic live-battery runbook

This is the allowed, reproducible workflow for Friday's recursive synthetic live
acceptance. It never uses the live archive or a real Telegram recipient. Model,
embedding and reranker traffic is restricted to the three configured numeric local
endpoints; every pass runs in an isolated filesystem and network namespace.

## Acceptance contract

One official battery is exactly ten independently isolated passes with twenty cases
per pass. A pass is complete only when all twenty sealed cases were dispatched once,
the result has the expected schema, and pass plus post-shutdown reconciliation are
cryptographically bound and clear.

The frozen manifests enforce:

- pass IDs `P01` through `P10`, ten distinct blocks and the fixed profile order;
- twenty normalized-unique questions per pass and 200 unique case IDs per battery;
- no normalized or semantic wording overlap between batteries A and B;
- synthetic markers on every question;
- no harness repair, resume, retry or failed-case resubmission.

Any non-empty case `failure_codes`, false privacy-canary verdict, worker/transport
failure, incomplete pass, reconciliation mismatch, candidate/runtime identity drift,
or aggregate count mismatch makes the battery red. Classification as `product`,
`oracle`, `transport` or `infrastructure` explains ownership of the repair; it does
not turn a red run green.

The recursive cycle is accepted only by one clean official A+B pair on one unchanged
released candidate: A must be 200/200 before B starts, B must then be 200/200, all
twenty runtime hashes must agree, and `pair_clean` must be true.

## Prerequisites

- Linux with executable `/usr/bin/bwrap` and `libseccomp.so.2`;
- the locked development environment and Playwright Chromium from the release
  checklist;
- enabled local LLM and embeddings plus a configured local reranker;
- numeric private/loopback URLs for all three endpoints (hostnames and public IPs are
  rejected);
- `FRIDAY_ENV_FILE` names the absolute, regular, non-symlink deployed config owned
  by the current euid with exact mode `0600` when it is not `./.env.local`; never
  copy that file into the repository or a run directory;
- `data/live-battery-runs/` remains Git-ignored and supports exact `0700` directories
  and `0600` files;
- every intended new release file is already in the exact Git index allowlist before
  the final candidate audit; adding a path to the index afterward changes the sealed
  inventory and invalidates the evidence;
- no source edits, staging, commit, deployment or configuration changes while a gate
  or battery is running.

Model-free manifest and candidate-binding preflight:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py --audit-only
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py --suite all --audit-only
```

Both commands must exit zero. The first must report two batteries, twenty passes and
400 cases. The second must report eight selected passes and 160 cases, and its
candidate inventory must bind both the battery instrument and the tracked acceptance
runner.

## Repair and pre-release gate

Finish all ten passes of a red battery before editing. Put every closed failure code
and synthetic case ID into one private ledger, independently classify every root,
then repair all roots as one batch. Raw questions, answers and worker logs never enter
Git, documentation, chat or public stdout.

After two independent read-only reviews are clear, reserve the exclusive gate slot,
set `QUALITY_GATE_BASE_SHA` to the full SHA of the previously accepted ancestor,
and run the canonical exact-release tier:

```bash
export FRIDAY_SYNCTHING_AMD64_TARBALL="${FRIDAY_SYNCTHING_AMD64_TARBALL:-$HOME/.cache/friday/test-assets/syncthing-linux-amd64-v2.1.3.tar.gz}"
candidate_sha="$(git rev-parse --verify 'HEAD^{commit}')"
base_sha="$(git rev-parse --verify "${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}")"
GOLDEN_JOURNEY_RELEASE_ROOT="$(readlink -f -- "$HOME/.jericho/current-release")" || exit 1
test -d "$GOLDEN_JOURNEY_RELEASE_ROOT" || exit 1
export GOLDEN_JOURNEY_RELEASE_ROOT
evidence_dir="$(mktemp -d -p /var/tmp friday-exact-evidence.XXXXXXXX)"
.venv/bin/python -I -B tools/quality_gate.py \
  --tier exact-release --candidate-sha "$candidate_sha" --base-sha "$base_sha" \
  --evidence-dir "$evidence_dir"
```

This one invocation runs the closed `change + exact-release` partition with no
imported receipt. It uses the default 20 non-UI/4 UI topology, rejects every skip,
and must create `quality-gate-summary.json`. See
[`QUALITY_GATE_TIERS.md`](QUALITY_GATE_TIERS.md) for private `/var/tmp` projection,
cleanup, evidence and external same-wheel measurement rules. Do not overlap it with
Mainline's gate or another battery.

Select and privately validate the deployed model configuration without printing it:

```bash
test -n "${FRIDAY_ENV_FILE:-}"
test -f "$FRIDAY_ENV_FILE" && test ! -L "$FRIDAY_ENV_FILE"
test "$(stat -c %a -- "$FRIDAY_ENV_FILE")" = 600
test "$(stat -c %u -- "$FRIDAY_ENV_FILE")" = "$(id -u)"
```

Then run both release-blocking live slices from one immutable candidate snapshot:

```bash
umask 077
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py \
  --env-file "$FRIDAY_ENV_FILE" --suite all --concurrency 4
```

This command dispatches A-P01/P02/P04/P08/P09/P10 (120 cases) and A-P06+B-P06
(40 cases), once each and without retry. Exit zero requires focused 120/120, P06
40/40, exact privacy/network/tool/effect/permission and pass/tail/combined
reconciliation, one candidate digest and one runtime identity across all 160 cases.

`--suite focused` and `--suite p06` exist for diagnosis. Separate runs do not replace
the canonical `--suite all` release evidence because they do not prove one shared
snapshot and runtime identity.

`--env-file` is consumed only by the outer live runner. Its path and raw contents
are not added to sanitized summaries, candidate/runtime projections or worker requests;
workers receive only the allowlisted model settings in memory and replace
`FRIDAY_ENV_FILE` with a nonexistent path inside their private scratch home.
`--audit-only` remains model-free and does not select or read an environment file.

Any source or relevant configuration change after either gate invalidates it. Rerun
the full source gate and canonical pre-release acceptance on the final digest.

## Release and official pair

After both gates are green on the same final candidate: commit exactly the already
staged allowlist without changing its paths or bytes, update only evidence that is
outside the candidate inventory, push `main`, deploy that commit, and verify health
plus a bounded local model check. Do not edit the candidate tree afterward.

Run the official pair:

```bash
umask 077
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py \
  --env-file "$FRIDAY_ENV_FILE" --both --concurrency 4
```

The runner completes all ten A passes even when individual cases fail. It starts B
only when A is wholly green. A red A or B returns exit code 4 and restarts the
recursive workflow at bulk classification and repair; it is never resumed or patched
in place.

## Private evidence and public proof

The default artifact directory is printed as an opaque `artifact_id` and created
beneath `data/live-battery-runs/`. A caller-supplied directory name is never echoed.
Every artifact directory is `0700`; every evidence, reconciliation and summary file
is `0600`; symlinks and special files fail closed.

Private pass evidence contains `raw-responses.jsonl`, `worker-runtime.log` when
present, `pass-reconciliation.json` and `tail-reconciliation.json`. Keep it local.
The safe release records are:

- `pre-release-sanitized-summary.json`, plus the focused and P06 sanitized summaries;
- official `battery-a/aggregate.json`, `battery-b/aggregate.json` and
  `pair-aggregate.json`;
- candidate, runner, manifest, runtime, evidence and reconciliation hashes;
- counts, closed failure codes, synthetic case IDs and privacy verdicts.

Documentation and change history may record only those sanitized facts and hashes.
