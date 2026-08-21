# Transactional orchestration

All commands are fail-closed. The switch defaults to read-only preflight; only
the explicit `-Execute` flag authorizes candidate/stable container changes.

```powershell
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedProxy.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedBindMountProjection.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedCapabilityProjection.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedPublisherObservation.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedNetworkProjection.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedCleanupProjection.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Test-AttestedReceiptSerialization.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Preflight-Qwen38V12Attested.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Switch-Qwen38V12Attested.ps1' -Execute
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Rollback-Qwen38V12Attested.ps1' -PreflightOnly
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Rollback-Qwen38V12Attested.ps1' -Execute
```

If a previous rollback left the sealed candidate containers stopped, the switch
correctly rejects their occupied names. Use the dedicated cleanup transaction;
its default is read-only, and only an explicit `-Execute` removes anything:

```powershell
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Cleanup-StoppedQwen38V12Attested.ps1'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Cleanup-StoppedQwen38V12Attested.ps1' -Execute
```

Cleanup accepts only the exact rollback-state v1 internal-only graph or the v2
two-network graph, the bound candidate IDs/images/labels, stopped state with
restart `no`, the exact model-volume attachment, and a healthy sole-publisher
stable graph. It removes the bound stopped proxy first and engine second, with
plain `docker container rm`; it never uses force, `compose down`, volume/image
removal, network disconnect, or network removal. It then proves both candidate
names absent, the model volume unattached, the Compose-owned internal network
exact and unattached, and the permanent publish bridge exact and unattached.
For a v1 cleanup only, it provisions that permanent bridge after removing the
old containers. A v2 cleanup never provisions, adopts, or replaces that bridge:
after container removal it reuses the receipt sealed in state, re-inspects the
live network, and rejects a missing or different ID even if every other field
and label is identical. An interrupted cleanup can safely re-run under the same
state.

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

The base candidate graph has one internal bridge only. The publish overlay adds
one external network and one attachment only: the proxy joins the exact durable
code-owned publish bridge with gateway priority 1, while the engine remains on
the internal bridge alone. The publish bridge requires exact top-level
IPv4/IPv6 booleans and Docker Desktop's exact empty driver-option map; the
Compose-owned internal bridge retains its two exact IPv4/IPv6 driver options.
Preflight verifies an existing publish bridge or
reports that `-Execute` will provision it. Provisioning happens before stable
drain and pins the exact name, 64-hex network ID, `bridge` driver, local scope,
top-level IPv4/IPv6 booleans, the network-specific exact driver-option map,
`internal=false`, `attachable=false`, non-ingress/non-config
status, and the complete four-label ownership set. A name collision, foreign
label, foreign attachment, wrong driver, wrong internal flag, or altered
Compose-owned internal bridge fails closed.

After proxy start, the runtime gate requires engine network count 1, proxy
network count 2, exact network IDs, internal/publish gateway priorities 0/1,
nonempty running IPv4 endpoints, internal attachments limited to the two
candidate siblings, and publish attachments limited to the proxy. It then
requires both configured and effective `0.0.0.0:8001 -> 8080/tcp` bindings plus
the exact sole Docker publisher. Every current-topology gate also rebinds the
live publish network ID to the sealed v2 receipt, so an otherwise identically
labelled replacement fails closed. The 120-second publisher wait remains only
for bounded registration settling: an empty set may wait, while any wrong,
duplicate, or multiple owner fails immediately. It cannot make an internal-only
container publishable. The publisher and network projection tests exercise the
accepted and negative matrices without Docker or container mutation.

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
probes. The switch explicitly loads `System.Net.Http` before preflight so its
HTTP client types resolve before mutation and the six-way probe; a missing
runtime dependency fails before mutation.
The six-way checkpoint has separate `six_way_probe`, `six_way_drain`, and
`post_six_way_gpu_headroom_convergence` stages. After all six responses and
three clear drain reads, only a valid free-VRAM reading below the unchanged
1,536 MiB floor may be retried, every two seconds for at most 30 seconds. Any
`nvidia-smi` command or response-schema error propagates immediately. The
terminal convergence journal record contains no request body: success records
the six-request count and verified free MiB, while a bounded timeout records
the final valid free MiB and the unchanged floor.
The long-context checkpoint similarly separates the accepted response,
`long_context_drain`, and `post_long_context_gpu_headroom_convergence`. After
three clear drain reads, it reuses the same valid-low-only two-second polling
and 30-second bound at the unchanged 1,536 MiB floor. Its body-free success
record identifies one request; command and response-schema failures still
propagate immediately instead of entering the convergence loop.
Before arming restart policies, the candidate engine is forcibly
restarted and the switch proves old-witness disappearance plus new canonical
witness and nonce rotation, then repeats identity, proxy, smoke, headroom,
sidecar, and sole-port-8001 gates.

Any post-mutation failure runs exact automatic rollback to the captured stable
container IDs. The publish bridge is permanent code-owned infrastructure: its
full identity and labels are sealed into rollback state v2, journals, preflight,
ready, and rollback receipts. Rollback never runs `docker network rm`,
`docker network disconnect`, or Compose `down`; it retains the exact network and
any stopped candidate proxy attachment as evidence. A later cleanup may remove
only the stopped candidate container IDs already bound in that state, then must
re-attest the same now-unattached publish network before reuse. It must never
force-disconnect, replace, or delete the durable bridge. The backend, Telegram
bridge, and all sidecars are never reconfigured or restarted by these scripts.

Switch and rollback serialize every journal or terminal record that may carry a
publish-network receipt with explicit JSON depth 12. This preserves the complete
nested four-label ownership object on Windows PowerShell 5.1. The receipt
serialization projection parses both scripts with the PowerShell AST, round
trips the nested receipt, and rejects any serializer that falls back to the
depth-2 default.

## Frozen Linux-to-Windows transport

The code-owned sync wrapper is fail-closed and defaults to a local-only plan. It
pins the SSH client key, the Windows ED25519 host key, the transport manifest,
the PowerShell applier, every exact live predecessor byte set (including an
already-applied bootstrap), and every new frozen byte set.
It uses a flat SHA-addressed archive, verifies it again after receipt and
expansion, and applies files under the same exclusive switch lock. Each target
must be absent only when explicitly declared new, or equal the exact old or new
SHA-256. Existing-file replacement uses a same-directory temporary file and a
deterministic, code-owned backup path. Windows PowerShell 5.1 receives that real
backup path in `File.Replace`; the applier proves the new target and exact old
backup before deleting only that backup. A retry accepts and converges the one
post-replacement crash state (new target plus exact-old backup); any other
backup or sync-temporary residue, hash, or target state fails closed. The
new-file `File.Move` path is unchanged. A pinned native test performs an actual
existing-file replacement and crash/retry matrix under Windows PowerShell 5.1
before the applier may inspect or mutate live targets. Payloads land first,
`CORE-SHA256SUMS` lands only after all changed CORE members, and
`ORCHESTRATION-SHA256SUMS` lands last. Runtime state and journals are not
transported or deleted, and the applier has no network, container, volume, or
image action.

The verified endpoint is `admin@192.168.1.78` on default TCP/22. The key is
`/home/jericho/.ssh/friday_win_audit_ed25519`; its public fingerprint is
`SHA256:vhJUpURIJLODWZdo8LU8qnTMbLir86/J5tzl8VWp5+A`. Host-key checking is
strict against `/home/jericho/.ssh/known_hosts`; the pinned Windows ED25519
fingerprint is `SHA256:wfOf57TOtNhTuQ6OAQUcWhMF47C8FWeUhku2gSAe6mY`.
The wrapper permits only `ssh-ed25519`, disables the global known-hosts file and
host-key updates, and verifies those effective settings with local-only
`ssh -G` before either a plan or a connection.

From the frozen Linux checkout, use this exact order. The two
`--remote-preflight` calls write only SHA-addressed staging evidence; they do not
change live bundle files. No remote command is run by a plain `--phase` plan.

```bash
sync=handoffs/SGLang-Qwen38-V12-Attested/Sync-Qwen38V12AttestedBundle.sh

"$sync" --phase bootstrap
"$sync" --phase bootstrap --remote-preflight
"$sync" --phase bootstrap --execute
```

The bootstrap phase contains only `AttestedBundle.Common.ps1`,
`Cleanup-StoppedQwen38V12Attested.ps1`, and
`docker-compose.publish-8001.yml`. It deliberately precedes cleanup so the
legacy v1 rollback state can be cleaned without copying or deleting that state.
Invoke cleanup through the verified PowerShell encoding convention:

```bash
ssh_base=(
  ssh -F /dev/null
  -i /home/jericho/.ssh/friday_win_audit_ed25519
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile=/home/jericho/.ssh/known_hosts
  -o GlobalKnownHostsFile=/dev/null
  -o HostKeyAlgorithms=ssh-ed25519
  -o UpdateHostKeys=no
  -o IdentitiesOnly=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o PreferredAuthentications=publickey
  -o ProxyCommand=none
  -o ProxyJump=none
  admin@192.168.1.78
)

cleanup_preflight=$(iconv -f UTF-8 -t UTF-16LE <<'PS' | base64 -w0
$ErrorActionPreference = 'Stop'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Cleanup-StoppedQwen38V12Attested.ps1' -PreflightOnly
PS
)
"${ssh_base[@]}" \
  "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $cleanup_preflight"

cleanup_execute=$(iconv -f UTF-8 -t UTF-16LE <<'PS' | base64 -w0
$ErrorActionPreference = 'Stop'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Cleanup-StoppedQwen38V12Attested.ps1' -Execute
PS
)
"${ssh_base[@]}" \
  "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $cleanup_execute"

"$sync" --phase full --remote-preflight
"$sync" --phase full --execute
```

After full sync, validate both SHA manifests and all six native, Docker-free
projection scripts before the ordinary read-only deployment preflight. The
following encoded invocation performs validation and preflight only; it has no
`-Execute` switch:

```bash
validate_preflight=$(iconv -f UTF-8 -t UTF-16LE <<'PS' | base64 -w0
$ErrorActionPreference = 'Stop'
$root = 'D:\jarvis-gpt\qwen38-v12-attested-bundle'
foreach ($manifestName in @('CORE-SHA256SUMS', 'ORCHESTRATION-SHA256SUMS')) {
    foreach ($line in Get-Content -LiteralPath (Join-Path $root $manifestName) -Encoding ascii) {
        if ([string]$line -cnotmatch '^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$') {
            throw "Non-canonical SHA manifest row: $manifestName"
        }
        $actual = (Get-FileHash -LiteralPath (Join-Path $root $Matches[2]) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -cne [string]$Matches[1]) { throw "SHA mismatch: $($Matches[2])" }
    }
}
foreach ($name in @(
    'Test-AttestedBindMountProjection.ps1',
    'Test-AttestedCapabilityProjection.ps1',
    'Test-AttestedPublisherObservation.ps1',
    'Test-AttestedNetworkProjection.ps1',
    'Test-AttestedCleanupProjection.ps1',
    'Test-AttestedReceiptSerialization.ps1'
)) {
    & (Join-Path $root $name)
}
& (Join-Path $root 'Preflight-Qwen38V12Attested.ps1')
PS
)
"${ssh_base[@]}" \
  "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $validate_preflight"
```

Only after that output is exact and retained for review is the GO mutation a
separate, explicit invocation:

```bash
switch_execute=$(iconv -f UTF-8 -t UTF-16LE <<'PS' | base64 -w0
$ErrorActionPreference = 'Stop'
& 'D:\jarvis-gpt\qwen38-v12-attested-bundle\Switch-Qwen38V12Attested.ps1' -Execute
PS
)
"${ssh_base[@]}" \
  "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $switch_execute"
```
