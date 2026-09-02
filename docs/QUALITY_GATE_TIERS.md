# Quality-gate tiers

`tools/quality_gate.py` is the single controller. Its checked-in inventory assigns
every exact test function to one product invariant, one execution kind and one tier,
then closes its parameter set with an exact count and digest. Unknown or stale
modules/functions, parameter drift, overlaps, collection drift and every skip fail.
One authoritative raw collection is classified once; selected JUnit node IDs must
equal that closed classification and remain explicit in run evidence.

| Tier | Trigger | Contract |
| --- | --- | --- |
| `change` | Public push/PR/manual Actions or operator-local | Committed-content checks and the bounded deterministic change set; non-certifying. |
| `exact-release` | Operator-local release only | `change` plus the remaining exact checks in one invocation, using one candidate wheel. |
| `nightly` | Private operator-local observation only | Seven external/host observations; never release certification or public logs. |

All UI nodes, deterministic representatives, owner-shell/process containment and
fault boundaries remain release-blocking. Nightly receives only authenticated host
observations and approved full-breadth measurements, not unproven deterministic cuts.
Exact host nodes require the provisioned Ubuntu 26.04 release host with package-owned
`/usr/sbin/apparmor_parser`, Bubblewrap, LibreOffice, nmap and
`/usr/bin/{printf,yes,sleep,true,test}`. The controller observes this fixed contour;
it never installs packages, loads policy or weakens global userns hardening.

`exact-release` always executes both its `change` and exact buckets. It cannot import
a prior summary or receipt as proof that either bucket ran. Diagnostic `--phase` and
`--dry-run` selections are not tier evidence.

## Direct commands

Commands require an exact candidate commit, an existing empty private evidence
directory outside the checkout, and a strict ancestor base for `change` and
`exact-release`:

```bash
umask 077
export FRIDAY_SYNCTHING_AMD64_TARBALL="${FRIDAY_SYNCTHING_AMD64_TARBALL:-$HOME/.cache/friday/test-assets/syncthing-linux-amd64-v2.1.3.tar.gz}"
candidate_sha="$(git rev-parse --verify 'HEAD^{commit}')"
base_sha="$(git rev-parse --verify "${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}")"
evidence_dir="$(mktemp -d -p /var/tmp friday-change-evidence.XXXXXXXX)"
.venv/bin/python -I -B tools/quality_gate.py \
  --tier change --candidate-sha "$candidate_sha" --base-sha "$base_sha" \
  --evidence-dir "$evidence_dir"
```

The exact host must provide that regular cached archive (or an explicit absolute
override). The installer test authenticates its pinned SHA-256 before extraction;
the isolated gate maps only this named asset into the test environment.

```bash
umask 077
candidate_sha="$(git rev-parse --verify 'HEAD^{commit}')"
base_sha="$(git rev-parse --verify "${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}")"
GOLDEN_JOURNEY_RELEASE_ROOT="$(readlink -f -- "$HOME/.jericho/wheel-only-releases/a9ef8565c80592275d61c16f293c7df16fb6aa89")" || exit 1
test -d "$GOLDEN_JOURNEY_RELEASE_ROOT" || exit 1
export GOLDEN_JOURNEY_RELEASE_ROOT
GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT="$(readlink -f -- "$HOME/.jericho/runtime/release-tools-020798/production-observation-private-020798-a9ef8565.json")" || exit 1
GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT_SHA256="7bb8c293fb6909d09bab268cfb13522102f90e8be0c4ae86ec71f258aee1128d"
test -f "$GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT" || exit 1
export GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT
export GOLDEN_JOURNEY_PRODUCTION_OBSERVATION_ARTIFACT_SHA256
evidence_dir="$(mktemp -d -p /var/tmp friday-exact-evidence.XXXXXXXX)"
.venv/bin/python -I -B tools/quality_gate.py \
  --tier exact-release --candidate-sha "$candidate_sha" --base-sha "$base_sha" \
  --evidence-dir "$evidence_dir"
```

`GOLDEN_JOURNEY_RELEASE_ROOT` is the immutable runtime identity pinned by the
canonical sanitized receipts. Never substitute the mutable `current-release`
symlink: a newer production runtime is not evidence for those receipts.
The private production-observation artifact is the matching external Release
Captain authority; its pinned digest is validated without copying its body into
the checkout or public gate evidence.

Nightly is never routed through public Actions. Run it only in a private operator
session whose readable absolute `QUALITY_GATE_REAL_BACKUPS_DIR`, executable
`QUALITY_GATE_SYNCTHING_BINARY` and `QUALITY_GATE_POWERSHELL_BINARY` are authenticated
by the controller. Set `QUALITY_GATE_POWERSHELL_SHA256` to the operator-reviewed
lowercase 64-hex digest of that exact binary. Missing, substituted or drifting assets
are failures, not skips. Arbitrary pytest output stays private; the summary retains
only binary digests/sizes/modes and an order-independent backup count/byte/digest.

```bash
umask 077
candidate_sha="$(git rev-parse --verify 'HEAD^{commit}')"
evidence_dir="$(mktemp -d -p /var/tmp friday-nightly-evidence.XXXXXXXX)"
: "${QUALITY_GATE_POWERSHELL_SHA256:?set reviewed PowerShell SHA-256}"
.venv/bin/python -I -B tools/quality_gate.py \
  --tier nightly --candidate-sha "$candidate_sha" --evidence-dir "$evidence_dir"
```

The controller authenticates the candidate, materializes the required Git objects
into a short private `0700` projection beneath `/var/tmp`, and executes from that
projection. It performs ordinary bounded descendant and scratch cleanup on every
exit. The retained evidence directory is separate. This runner boundary does not
claim protection from a hostile process already operating as the same OS user.

Exact release fixes 20 non-UI workers and 4 UI workers and fails below 24 effective
CPUs or 32 GiB initially free scratch. Public hosted `change` explicitly uses 4/1,
requires four CPUs and 8 GiB free `/var/tmp` after setup, retains pytest temp paths
only for failures, and remains non-certifying. Its summary is the topology-matched
peak measurement; if the hosted contour cannot fit, it must be removed rather than
represented as release evidence.

## Inventory maintenance

Produce the retained authoritative collection through the same isolated serial
pytest bootstrap, then byte-check the inventory. The output must be an absent
absolute path outside the checkout:

```bash
maintenance_dir="$(mktemp -d -p /var/tmp friday-inventory.XXXXXXXX)"
collection="$maintenance_dir/all-tests.json"
.venv/bin/python -I -B tools/quality_gate.py --inventory-collection "$collection"
.venv/bin/python -I -B tools/quality_gate_inventory.py --collection "$collection" --check
```

For deliberate new functions, replace `--check` with `--write --declare FUNCTION
INVARIANT TIER KIND MAX_RUNTIME_SECONDS SCRATCH_MIB` once per function. Parameter-only
drift needs `--write` without declarations. Review the TSV diff; removals and
undeclared functions fail.

## Evidence and measurement

Controller success creates `quality-gate-summary.json` in the requested directory.
The canonical summary binds candidate/base/tier, inventory digest, wheel digest,
the full classified partition, exact executed node durations, wall and CPU time,
peak RSS, peak scratch bytes, retries and worker topology. Failed, errored or skipped
nodes cannot produce a passing summary. Raw test output and observation bodies are
not evidence fields. Its process metrics freeze before summary composition and are
diagnostic, not terminal performance acceptance.

Non-UI and UI receive isolated homes, temporary directories and pytest basetemps.
Each function declares a planning budget; the summary records their group total and
a 0.5-second sampled regular-file peak delta above a fixed pre-group baseline. This
includes sampled writes beside the direct group directories. Polling can miss a
short-lived allocation, so neither the sampled peak nor the declared budget is a
quota or certification boundary. A separate 5-second sampler uses the S6 baseline
semantics across the full gate lifetime for comparable measurement only.

CI publishes only the descriptor-bound controller summary. It does not accept or
upload a mutable terminal-timing sidecar. The one-off performance comparison is an
operator-owned measurement outside the canonical evidence directory; its terminal
wall/CPU/RSS cover the complete controller invocation and remain non-certifying.

Performance acceptance is an external comparison, not another controller tier.
Reserve the exclusive gate slot; never overlap a quality gate, Mainline gate or
Playwright run on the same host. Compare the candidate summary with the authenticated
S6 baseline only after proving both used the same wheel bytes. That baseline recorded
744.735532 wall seconds, 1,199,344 KiB peak RSS, 13,963,775,721 peak scratch bytes,
four outer attempts and zero final-run retries. It used 12 non-UI/1 UI workers; the
candidate default is 20/4, so the external comparison must report that topology
change. The shared wheel is:

```text
sha256:954641e37fc8958a8b459f65f78e61225709be8d20e1b3979fcb22e378018aca
```

An ordinary exact-release run builds, verifies, clean-installs and tests its candidate
wheel and emits the candidate's certifying summary. Run it first. A separate one-off
comparison still builds, verifies and clean-installs that normal wheel, then makes the
delta-7 same-input requirement executable by appending:

```text
--comparison-wheel-sha256 954641e37fc8958a8b459f65f78e61225709be8d20e1b3979fcb22e378018aca \
--comparison-wheel-epoch-sha ff8c62926e7c7ea9cfcd53c460f9a0608d83621c
```

After the normal path, the controller makes a second independent exact projection of
the authenticated comparison commit, rebuilds and verifies its wheel, and requires
its digest to match the supplied baseline digest. It clean-installs that byte-identical
wheel as the selected-test runtime for this comparison run. The comparison commit is
not the change-coverage `--base-sha` and must remain the S6 release commit shown above
after integration or rebase. The normal candidate wheel digest may differ (README is
wheel metadata); it remains covered by build, verifier and clean-install boundaries.
This exact epoch/digest pair also selects the closed
`legacy-git-archive-umask-0002-v1` reconstruction profile: only the comparison
projection recreates the historical Git-archive modes and child build umask. The
candidate and the gate process retain canonical `0022`; any unknown pair fails closed.
Because selected tests use the S6 wheel, this second output has the distinct
`friday.quality-gate-measurement.v1` schema, `result=measured` and
`certification_eligible=false`; it cannot certify the candidate or replace the first
summary. The external comparison records attempts/retries and hashes both outputs. The target is
`candidate_terminal_wall_seconds * 10 <= baseline_terminal_wall_seconds * 7`, using
symmetric external process timing, with no coverage loss. A baseline summary or
timing result never certifies the candidate.
