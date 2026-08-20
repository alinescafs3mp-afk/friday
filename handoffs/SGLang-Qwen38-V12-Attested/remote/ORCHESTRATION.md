# Transactional orchestration

All commands are fail-closed. The switch defaults to read-only preflight; only
the explicit `-Execute` flag authorizes candidate/stable container changes.

```powershell
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedProxy.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedBindMountProjection.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedCapabilityProjection.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedPublisherObservation.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Preflight-Qwen38V12Attested.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Switch-Qwen38V12Attested.ps1' -Execute
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Rollback-Qwen38V12Attested.ps1' -PreflightOnly
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Rollback-Qwen38V12Attested.ps1' -Execute
```

The proxy test uses no host port and removes its isolated test container. It
sources the API key only inside PowerShell from the exact stable proxy and does
not print or persist that key.

Bind-source attestation accepts only the code-owned Windows spelling, its exact
forward-slash Docker spelling, or the one exact Docker Desktop
`/run/desktop/mnt/host/<lowercase-drive>/...` projection. It does not normalize
the inspected source: mixed separators, traversal, case changes, another drive,
or any near-miss remain fail-closed. The projection test exercises this matrix
without Docker or container mutation.

Runtime proxy capability attestation accepts exactly one of two complete,
code-owned sets: the Compose spelling (`CHOWN`, `DAC_OVERRIDE`, `SETGID`,
`SETUID`) or Docker's exact `CAP_`-prefixed projection of all four names. Set
membership is ordinal and order-independent; inspected names are never
case-folded or stripped of prefixes. Mixed spellings, missing, duplicate, extra,
wrong-case, and near-prefix capabilities remain fail-closed. The capability
projection test exercises this matrix without Docker or container mutation.

Docker Desktop may report the exact proxy healthy before its publisher index
shows port 8001. Candidate startup therefore waits at most 120 seconds while the
publisher set is empty. Any wrong, duplicate, or multiple publisher fails
immediately; the wait never tolerates a competing owner. The publisher
observation test exercises the accepted, pending, and rejected sets without
Docker or container mutation.

The switch captures and seals the exact current stable container IDs immediately
before mutation. It never invokes the mutable image builder and requires the
code-owned engine/proxy image IDs from the build receipt and local image store.
The rendered Compose services launch directly by those `sha256:` image IDs;
mutable local tags are never execution inputs.

Before stable drain, `-Execute` creates the exact sealed model volume only when
absent, copies and verifies the source snapshot, then performs an independent
read-only verification. A pre-existing volume is verify-only. If sealing a
volume newly created by that invocation fails, cleanup may remove only that
exact labelled, unattached volume; it never removes a pre-existing volume.

Acceptance requires the closed proxy negative-path matrix, exact 40,960-token
and six-request capacity, full decode CUDA graphs for batches 1..6, VRAM
release/headroom gates, text/JSON-schema, six-way, long-context, image, and soak
probes. Before arming restart policies, the candidate engine is forcibly
restarted and the switch proves old-witness disappearance plus new canonical
witness and nonce rotation, then repeats identity, proxy, smoke, headroom,
sidecar, and sole-port-8001 gates.

Any post-mutation failure runs exact automatic rollback to the captured stable
container IDs. The backend, Telegram bridge, and all sidecars are never
reconfigured or restarted by these scripts.
