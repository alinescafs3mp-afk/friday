# Friday 1.0 acceptance contract

This is the single entry document for RC → 1.0 user acceptance. It does **not**
replace [`QUALITY_GATE_TIERS.md`](QUALITY_GATE_TIERS.md),
[`LIVE_BATTERY_RUNBOOK.md`](LIVE_BATTERY_RUNBOOK.md) or
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md). Live status and defects live only
in [`outer_sol/PROJECT_BACKLOG.md`](../outer_sol/PROJECT_BACKLOG.md).

**Готовность этой батареи не означает, что Friday принята как 1.0.**

Machine matrix: [`tools/release_1_0_capability_matrix.json`](../tools/release_1_0_capability_matrix.json).
Wrapper: [`tools/release_1_0_acceptance.py`](../tools/release_1_0_acceptance.py).
Additional journeys: [`tools/release_1_0_live_journeys.py`](../tools/release_1_0_live_journeys.py).

## Scope proposed for Astra / owner freeze

| Class | Capabilities |
| --- | --- |
| Required 1.0 | Dialogue, files/ingestion, archive search, Inbox/KG, Admin UI, Telegram adapter, web/mixed, backup/restore, privacy, generated artifacts, CLI, primary-only when secondary is absent |
| Optional when enabled | Engineer Mode, Coding Mode, Host Capability Plane, secondary assist |
| Beta, not 1.0 | Obsidian core; physical Android remains unfinished |
| Out of scope | Companion plugin, Pandora P0H deletion, Gemini parity, off-machine mirror target, provider credential rotation |

A broken promised function cannot be relabeled beta to obtain a green table.
Owner-parked proofs stay `NOT_RUN` / `OUT_OF_SCOPE` and never count as PASS.

## Existing gates (unchanged, still mandatory)

1. `tools/quality_gate.py --tier exact-release` — closed inventory, no skips.
2. `tools/synthetic_live_acceptance.py --suite all` — 160 executions of sealed A/B cases (120 focused + 40 P06). Not 160 extra unique scenarios.
3. `tools/synthetic_live_battery.py --both` — official 200+200; **B does not start if A is red**.

New R10 cases are an additional identified set. Sealed A/B bytes are not rewritten.
The ten additional pytest functions are declared in `tools/quality_gate_inventory.tsv`
(`change` / `unit`). They do not replace exact-release or the 160/A+B live gates.

## Layers

| Layer | What it proves | What it is not |
| --- | --- | --- |
| Deterministic | Unit/integration/UI/security and R10 isolated TestClient journeys | Live-model quality |
| Isolated live | Real candidate, real DB/indexes/workers, local LLM endpoints | Production home, live Telegram singleton |
| User UI | Playwright Chromium Admin UI | Firefox (not a 1.0 promise) |
| Deployment/device | Native install/restore, dedicated test bot, physical Android | Working owner phone or live bot |

## GO / NO-GO / INCOMPLETE

Proposal (do not weaken any stricter existing rule):

- **GO** only as a conjunction: every required capability listed, every required case executed in the required layer, no `SKIP`/`BLOCKED`/`NOT_RUN`/`MANUAL_PENDING` in the required set, no open critical/high defects, exact-release + 160 + clean A+B + additional R10 live, sealed candidate/wheel/runtime identities.
- Any tenant leak, unauthorized effect, silent data loss, false completion or duplicate irreversible action is **NO-GO**.
- Missing proof is **INCOMPLETE**, which also forbids declaring 1.0.

This wrapper **never prints GO**. Astra records the verdict in the single backlog after the checkpoint.

## Exact commands

Model-free harness (safe to run anytime; does not occupy the full-gate slot):

```bash
umask 077
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --audit-only
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --preflight
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --negative-control
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py --audit-only
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py --suite all --audit-only
.venv/bin/python -I -B -m pytest -q tests/test_release_1_0_acceptance.py tests/test_release_1_0_journeys.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --plan diagnostic-baseline
```

Diagnostic baseline / final (exclusive native/UI/model-heavy slot; Astra coordinates).
Do not overlap another quality gate, Mainline gate or Playwright run. Do not use
the production home or the live Telegram singleton.

```bash
umask 077
export FRIDAY_SYNCTHING_AMD64_TARBALL="${FRIDAY_SYNCTHING_AMD64_TARBALL:-$HOME/.cache/friday/test-assets/syncthing-linux-amd64-v2.1.3.tar.gz}"
candidate_sha="$(git rev-parse --verify 'HEAD^{commit}')"
base_sha="$(git rev-parse --verify "${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}")"
GOLDEN_JOURNEY_RELEASE_ROOT="$(readlink -f -- "$HOME/.jericho/wheel-only-releases/8b6a8c13ce54b8b07192cb6f5b820953da4efcb5")" || exit 1
test -d "$GOLDEN_JOURNEY_RELEASE_ROOT" || exit 1
export GOLDEN_JOURNEY_RELEASE_ROOT
GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT="$(readlink -f -- "$HOME/.jericho/runtime/release-tools-020800/production-observation-private-020800-75b165a2.json")" || exit 1
GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT_SHA256="207aad4458d048c0abd9c52befd05b7b29bda7988e8c62fa62fb3a5b6eac7f66"
test -f "$GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT" || exit 1
export GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT
export GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT_SHA256
evidence_dir="$(mktemp -d -p /var/tmp friday-exact-evidence.XXXXXXXX)"
.venv/bin/python -I -B tools/quality_gate.py \
  --tier exact-release --candidate-sha "$candidate_sha" --base-sha "$base_sha" \
  --evidence-dir "$evidence_dir"
```

Then, on the same frozen candidate, with a deployed `FRIDAY_ENV_FILE` of mode `0600`
that is **not** the production home:

```bash
test -n "${FRIDAY_ENV_FILE:-}"
test -f "$FRIDAY_ENV_FILE" && test ! -L "$FRIDAY_ENV_FILE"
test "$(stat -c %a -- "$FRIDAY_ENV_FILE")" = 600
umask 077
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py \
  --env-file "$FRIDAY_ENV_FILE" --suite all --concurrency 4
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py \
  --env-file "$FRIDAY_ENV_FILE" --both --concurrency 4
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_live_journeys.py \
  --env-file "$FRIDAY_ENV_FILE" --run-live
```

A red A still finishes all ten A passes and must not start B. Additional R10 live
may run after a red required set only when isolation/safety preconditions hold;
that does not make the aggregate certifying.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Wrapper audit/preflight/negative-control green |
| 2 | Matrix/sealed-audit/preflight failure |
| 4 | Canonical live battery red (existing runners) |
| 5 | Additional live `NOT_RUN` (missing exclusive slot or contour) |
| 70 | Live-acceptance teardown hard exit |

Canonical quality-gate and live-battery codes are unchanged. The wrapper cannot
override them.

## Evidence

Private traces stay under `data/live-battery-runs/` (`0700`/`0600`) or the
caller-supplied evidence directory. Public summaries may contain counts, IDs,
closed failure codes, hashes and privacy verdicts only. Raw questions, answers,
worker logs and secrets do not enter Git, docs or chat.

Per-case fields: run/case/capability IDs, suite/manifest/candidate identities,
layer, status, expected/observed, closed failure codes, root-cause class
(`product` / `harness` / `transport` / `environment`), timings, opaque proof
refs, cleanup, retries.

## Manual only where a safe contour does not exist

- Live Telegram round-trip: dedicated test bot, never the production singleton.
- Physical Android Obsidian: owner-parked; remains beta.
- Soak duration/load: freeze before the final run; a short smoke is not a soak.

## Known product gap this battery must not hide

First-turn Word/DOCX generation on live `0.208.57` fails when the source `.txt`
filename is spoken (`WORD003_MAKE_FILE_PRE_RENDER_REJECT_SOURCE_TXT_STEM`).
Case `R10-LIVE-DOC-WORD-FIRST-GEN` stays required with expected PASS. Diagnostic
baseline must record FAIL, not skip or relabel.
