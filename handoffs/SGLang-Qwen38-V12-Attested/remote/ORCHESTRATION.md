# Transactional orchestration

All commands are fail-closed. The switch defaults to read-only preflight; only
the explicit `-Execute` flag authorizes candidate/stable container changes.

```powershell
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Preflight-Qwen38V12Attested.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Switch-Qwen38V12Attested.ps1' -Execute
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Rollback-Qwen38V12Attested.ps1' -PreflightOnly
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Rollback-Qwen38V12Attested.ps1' -Execute
```

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
