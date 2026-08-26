# Qwen3.8 abliterated activation contract

This is the isolated, rollback-safe primary bundle for
`Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4` at revision
`43aa7ff5eef05ab50a3bfa6aca581085312c7a04`. The served alias remains
`dispatcher`; the code-owned deployment profile is v12.15.

The candidate owns distinct containers, networks, witness volume, sealed model
volume, derived images, and Compose project. The preserved v12.14 deployment is
the only rollback predecessor. Its bundle, containers, images, and model volume
must not be renamed, overwritten, or deleted.

## Frozen identities

- Model manifest: `e5fa0d366c3bcf6546f9f3d0cb418b8e2530e2701a5a1506367f88fd08d1d1a4`
- Launch manifest: `ed18fc43f7a865dc0d01c568f22200fb71eebdcc2cef354f859860c966f3a19a`
- Engine image: `sha256:62ae2bb57a54a1dfcc33c05cdfd200cc69705ac94ad503cd4ec00a409804acaf`
- Proxy image: `sha256:2227ed08bc4360eea50b1bba31b0f07d5652ba63344a0ab0f135aec63fb680de`
- Sealed model volume: `jarvis-gpt-qwen38-abliterated-v12-attested-model-e5fa0d366c3bcf65`
- Compose project: `jarvis-gpt-qwen38-abliterated-v12-attested`

`CORE-SHA256SUMS` and `ORCHESTRATION-SHA256SUMS` bind the exact source bytes.
The model manifest binds all 18 inference payload files and 20,613,780,167
bytes. The build receipt binds the derived images to their immutable inputs and
pinned base-image IDs.

## Activation and rollback

Preflight is read-only. Only the explicit `-Execute` switch may stop the old
pair and publish the candidate:

```powershell
$root = 'D:\jarvis-gpt\qwen38-abliterated-v12-attested-bundle'
& (Join-Path $root 'Preflight-Qwen38AbliteratedV12Attested.ps1')
& (Join-Path $root 'Switch-Qwen38AbliteratedV12Attested.ps1') -Execute
```

The requested activation path intentionally runs only health, exact model
inventory, ordinary chat, native tool-call, witness, and idle checks. It does
not claim that the inherited soak, six-way, image, restart-epoch, or 40K
certification battery ran. The switch records `extended_acceptance_run=false`.
The runtime profile therefore remains `quick_smoke_only` and V12 admission stays
fail-closed until its coordinated release and fresh startup probes.

On success both candidate containers are armed with `unless-stopped`. Any
post-mutation failure before readiness automatically stops and disarms the
candidate, restores the captured stable container IDs and restart policies, and
proves that the stable proxy is again the sole port-8001 publisher.

Manual rollback is also explicit:

```powershell
& (Join-Path $root 'Rollback-Qwen38AbliteratedV12Attested.ps1') -PreflightOnly
& (Join-Path $root 'Rollback-Qwen38AbliteratedV12Attested.ps1') -Execute
```

Rollback retains the stopped candidate containers, new images, sealed model
volume, witness volume, and networks as recoverable evidence. The cleanup
transaction can remove only the exact stopped candidate containers bound in the
rollback state; it never removes either model volume, image, or old bundle.

## Source transport

`Sync-Qwen38AbliteratedV12AttestedBundle.sh` is a create-new-only transport for
this isolated root. Its local plan performs no network connection. Remote
preflight stages and verifies a flat SHA-addressed archive under the pinned SSH
and host-key identities; `--execute` acquires the shared switch lock and creates
the exact root only when it is absent. It never patches or replaces an existing
bundle, and it has no container, image, volume, or network action.

```bash
sync=handoffs/SGLang-Qwen38-Abliterated-V12-Attested/Sync-Qwen38AbliteratedV12AttestedBundle.sh
"$sync"
"$sync" --remote-preflight
"$sync" --execute
```
