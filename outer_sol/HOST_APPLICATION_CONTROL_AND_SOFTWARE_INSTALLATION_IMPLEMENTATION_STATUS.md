# Host Capability Plane — implementation status

> Status: source release candidate; production acceptance pending
>
> Updated: 26 August 2026
>
> Design authority: `HOST_APPLICATION_CONTROL_AND_SOFTWARE_INSTALLATION_ARCHITECT_BRIEF.md`

## Release boundary

The production candidate implements the first bounded Host Capability Plane and
keeps every `FRIDAY_HOST_*` switch off by default. It is ready for an exact
Ubuntu 24.04 staging run, not for an untested direct production activation.
Normal Friday operation remains independent of both host-side services.

The release integrator must allocate a new product version and matching
`CHANGELOG.md` entry after merging this candidate. The branch deliberately does
not reuse the already released `0.207.27` identity or guess the version of the
parallel release line.

## Implemented

- Versioned canonical plans, authenticated request envelopes, executable and
  adapter attestations, signed receipts, bounded evidence projection, replay
  rejection and durable Host Action jobs/events (schema 43).
- Owner-only capability search/describe, action execution, status and
  cancellation. Disabled Host Control and an agent disconnected during registry
  composition expose no model tools; a later live disconnect fails calls softly
  until the next registry refresh/restart.
- A non-root `friday-host-agent` over an owner-only Unix socket. Reviewed argv
  runs in bounded transient `systemd --user` cgroups; there is no shell, PATH
  lookup, Docker socket, sudo credential or arbitrary environment surface.
- A separate root `friday-package-broker` with an exact APT plan/execute/status/
  cancel protocol. First-release policy admits only `nmap`; repository changes,
  arbitrary packages/options and generic command execution are absent.
- Bounded raw APT stdout/stderr is retained only as private content-addressed
  `0600` evidence. Signed receipts expose hashes, references and honest retained/
  total/completeness metadata; raw bytes never enter SQLite, the event journal,
  prompts or public projections.
- A crash after the APT effect is reconciled read-only against the exact package
  pre/desired/mixed snapshot. Desired state receives a separate signed
  reconciliation receipt and may resume without a second commit; pre-state is
  safe only to re-plan, while mixed/unavailable remains durable `unknown`.
- The literal continuation journey: missing `nmap` -> canonical APT simulation
  -> payload-bound human approval -> exact install and signed postcondition ->
  executable re-attestation -> automatic resume -> bounded authorized scan ->
  shared XML parser, evidence and coverage.
- Shared Engineer/Host nmap normalization, fixed `/usr/bin/nmap` argv builder,
  version/provenance checks, XML limits, target accounting and result contract.
- A real non-network application action for preinstalled `jq`: one actor-owned
  Raw JSON file is reauthorized, copied into an immutable per-job input grant,
  processed with a code-generated field-only jq program, and returned as an
  exact receipt-bound durable attachment available through message history and
  download. A current pending upload works in the same turn. Arbitrary jq source
  and host paths are not accepted; the original file is unchanged.
- Ubuntu installer/uninstaller, hardened user/system units, rootful Compose
  override, persistent private socket parent, exact backend/desktop UID/GID
  mapping, verified prebuilt-wheel-only offline install, permanent versioned
  venv preflight, atomic activation, exact failure/signal rollback, diagnostics
  and rollback instructions.
- A deterministic Host Control release-bundle builder binds the canonical wheel
  and exact closed deployment file set, Git blob types and executable modes to
  a clean Git commit. Verification uses an independently obtained archive
  SHA-256 and rejects altered wheels, extra/missing members, unsafe source
  types/modes and noncanonical archives.
- Public-network authority uses a short-lived plan/approval/job/actor-bound
  Ed25519 proof with a restart-safe immutable one-use ledger. The native agent
  recomputes the approval digest and re-verifies immediately before launch;
  the backend issues a fresh proof only when the queued action reaches its final
  authorization/policy seam. Backend handshake and diagnostics both pin the
  exact signer public-key digest.
- The backend action queue is bounded and leaves jobs durably in their
  pre-effect `planned`/`awaiting_approval` state until a slot is owned. Queue
  saturation or cancellation closes that job before send. A claimed approval
  can survive backend restart only when the exact immutable job still proves
  `request_sent` was never crossed; running/unknown jobs require reconciliation
  and cannot replay.
- Engineer Mode remains a separate owner-only defensive workbench with one
  current-turn target, pinned resolution, bounded DNS/TCP/TLS/HTTP/nmap
  observations and no-network bubblewrap artifact analysis. Artifact processes
  inherit a finite PID cgroup; production verification requires the exact
  512-task ceiling and does not use a private-`/proc` UID count that breaks the
  combined real-desktop-UID contour.

## Fail-closed and deferred surfaces

- Desktop control and one-shot execution are not implemented. Setting either
  reserved flag to `1` is a configuration error.
- The privileged package allowlist remains exactly `nmap`; `jq` must be
  preinstalled for the file action. Package removal/upgrade, repositories,
  Flatpak, Snap, pip/npm installers and declarative third-party adapters are
  deferred.
- ffmpeg, LibreOffice, MPRIS, GUI/accessibility and visual automation are
  deferred. No launch-only result is presented as functional control.
- Public-network Host Control remains separately default-off and requires an
  exact configured CIDR plus plan-bound approval. First production acceptance
  must use an owner-controlled private target.
- The Compose override supports rootful Docker with host user namespaces and an
  exact numeric UID/GID match. Rootless/subordinate-ID deployment is unsupported
  rather than weakening socket/key permissions.

## Acceptance state

Automated contract, policy, drift/replay, package, process, unknown-outcome,
shared-nmap, jq vertical, schema, deployment and diagnostics tests are part of
the canonical gate. Static coverage includes all three shipped Python package
roots: `friday`, `friday_host_agent` and `friday_package_broker`.

Production acceptance still requires the exact merged/versioned commit to pass:

1. `python tools/quality_gate.py` in the release environment;
2. the Ubuntu 24.04 preflight and unit verification in
   `deploy/host-control/README.md`;
3. disabled/disconnected fail-soft startup, including backend restart while the
   agent socket itself is absent;
4. a pending-upload preinstalled-jq owner-file transformation with unchanged
   input and exact durable output/evidence/download/replay bytes;
5. rejected and approved fresh `nmap` install journeys on a disposable test host;
6. drift/replay and connection-loss reconciliation without duplicate effects;
7. an owner-controlled private-subnet scan with complete target accounting.

Until those receipts are archived, keep Engineer Mode, Host Control, package
install and public-network flags at `0`. A failed staging run rolls back by
disabling the flags and applying the documented Compose/unit uninstall sequence;
it must never be worked around by broadening socket modes, UID allowlists or
package policy.
