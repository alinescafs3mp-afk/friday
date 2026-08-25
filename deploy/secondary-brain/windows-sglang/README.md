# Optional Friday secondary brain: Windows + SGLang

This bundle runs one detachable, advisory GPT-OSS node on the Windows laptop at
`192.168.1.35`. Friday remains primary and final: the node has no tools, effects,
publication authority or V12 authority, and loss of the laptop must degrade to
the normal primary path.

The immutable native-MXFP4 base is exact; only a profile selected from the
closed candidate surface may vary its reviewed engine settings:

- model `openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee`;
- sealed volume `friday-secondary-source-gptoss20b`, mounted read-only at `/source`;
- model path `/source/snapshot` and manifest `/source/source-manifest.json`;
- SGLang `0.5.17` image
  `lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405`;
- image config
  `sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc`;
- SGLang source revision `29481685462732237d80d86076d6563e1f658102`;
- native `mxfp4` weights, BF16 model and KV dtype, `flashinfer_mxfp4` MoE on
  SM120, explicit CPU transport for the unused multimodal feature channel, one
  running request and no weight/KV CPU offload. The exact provisional finalist
  uses 4,096 total context tokens, 512 output tokens,
  `mem_fraction_static=0.96`, chunked prefill 256, page size 1, Triton
  attention, PyTorch sampling, radix/overlap cache, hybrid SWA ratio 0.80, a
  full decode CUDA graph at batch one and no prefill graph.

The earlier community checkpoint, internal ModelOpt NVFP4 conversion and patched
SGLang 0.5.16 image are rejected. Their conversion and calibration utilities
have been removed from this deployment bundle.

## Invariants

- Compose uses the exact `repo@sha256` image with `pull_policy: never`.
- Only the private-CA HTTPS gateway publishes TCP 8443. SGLang TCP 30000 stays
  on an internal Docker network.
- Gateway and SGLang use different file-backed bearer tokens. Neither belongs
  in `.env`, command lines, reports or version control.
- The source volume is read-only. Before importing SGLang or creating CUDA
  state, the launcher rehashes the exact source manifest and all 14 files
  (13,789,264,674 bytes).
- The runtime profile is canonical schema `friday.secondary-runtime-profile.v7`.
  It binds source, runtime image/config/OCI identities, backend selection,
  context, memory, graph policy, both exact SGLang compatibility files and every
  acceptance receipt. The code-owned sampler is the pinned 0.5.17 source plus
  upstream PR #35830: TP=1 skips the otherwise unnecessary grammar token-ID
  collective, while TP>1 retains the original synchronization semantics. Its
  exact SHA-256 is
  `5ddc5343c1ac368052046bc467d0d8fbd7fe3288b6ea8f88beb89cd4c8962d2e`.
- The accepted-profile registry stays empty until protocol, quality, capacity,
  restart, soak and failure batteries all pass.

## Current checkpoint

The current candidate source is Friday `0.207.11` in this revision. Its last
accepted deployed predecessor is Friday `0.207.10`, schema 39, at commit
`aaae455a3eec6024c1e4e338d8f00b31ee90f995`, with tree
`4f5c5e9a130e33f47fbf8f9282362f77b18b8f625d00f313b0cda4124d7ab76e`, wheel
`a563ad94c678ca5332f0cfe142ef65a18c6cc4a12f7e07b9d64c2734d06181f6` and
fallback `f1426ca561f8914574cebf3a69f8dde83f79b568` / tree
`eb8102ccf759b0f2a2d9a0a38584d9cda0c4938f14d389ac55246d87e536e6f7`. The
secondary feature remains default-off. The exact observed/accepted hardware
receipts are respectively
`7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3` and
`0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f`.
The sealed model source is exact. The running provisional finalist is profile
`gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`
with candidate-manifest SHA-256
`51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439` and
engine binding
`2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`.
It is code-admitted only to discarded non-private `shadow/extract`. The
accepted registry is empty, `assist` remains blocked and production sends no
traffic to the laptop. The fresh epoch-D warm capacity-v2 trial
`capacity.v2.epoch-d.warm.7c1f742.json` passed all seven non-streaming exact
512-token repeats at runtime epoch `1787601267.06`; its SHA-256 is
`b317e964eced1c0a80d5d8f4cc7fcb388d60598c16dfbeb9f320f1076fa97719`,
median end-to-end completion rate is `108.497563` tokens/s and minimum free GPU
memory is 1,294 MiB. The bound epoch-D soak passed in 1,800.218 seconds with
4,467 requests, zero failures, 1,294 MiB minimum free GPU memory and 75 C peak;
`soak.epoch-d.full.7c1f742.json` hashes to
`852673984f6705c148d0a92957d3c2f2fd5360925b0fddd2225eb8b631a8983a`.
After a real cold restart, epoch `1787603294.09` passed the matching capacity-v2
protocol 7/7 at exactly 512 completion tokens: median end-to-end rate
`106.733375` tokens/s, minimum free GPU memory 1,296 MiB and receipt SHA-256
`9c60611b939098020faa4f9077debde3bec96c9ded2bffc3c3385fc94d5ffa87`.
The post-cold probe, 29/29 quality and quality-epoch receipts hash respectively
to `b3a88138d43ac799aa113e1b53f8cc9b0c0c106d0decaf71726a438e29ddeec5`,
`7bb0e3aa9b48dd95afdf8a1c226fa5b7eae6212f45f72966d82344cd3227e824`
and `b7e345962770e26f45b90f33c9ac7180be4c72dc4427247025a23fefe197a310`.
The verified capacity-v2 wrapper accepted the bound warm/cold/soak trio as
`519b5912428f491dc65928c5ba2d2e33a6408566fe5f3496501ce2e760b9205e`.
An earlier operational wrapper stopped on its superseded v1 schema assertion;
it produced no success claim. Pre-acceptance deterministic/controlled/physical
node evidence and profile acceptance remain pending; registration,
private-shadow/assist and the later product-linked physical cycle follow them.

## 1. Management access and host preflight

Use the existing key-only SSH alias `friday-secondary-brain`. Do not restore
password authentication. The host firewall permits SSH only from the primary
machine and later permits gateway TCP 8443 only from that same address.

Run inventory first without mutation:

```powershell
.\scripts\preflight.ps1
```

After the exact images are already local, run the complete image/GPU checks:

```powershell
$sglang = 'lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'

.\scripts\preflight.ps1 `
  -SglangImage $sglang `
  -RunGpuCanary -InspectSglangHelp -InspectGatewayImage `
  -OutputPath .\evidence\preflight.observed.json `
  -HardwareRuntimeReceiptOutputPath .\evidence\hardware-runtime.observed.json
```

The SGLang check requires the exact local config and OCI-manifest identities,
`linux/amd64`, the complete v0.5.17 launch-flag surface and a stopped Compose
selector canary. It never pulls an alternate image.

Review the observed hardware receipt, then let the promotion script create the
protected accepted copy without editing either file:

```powershell
.\scripts\accept-hardware-runtime-receipt.ps1 `
  -ObservedPath .\evidence\hardware-runtime.observed.json `
  -AcceptedPath .\evidence\hardware-runtime.accepted.json `
  -Apply

python .\scripts\accept_runtime_manifest.py `
  --template .\runtime-manifest.example.json `
  --preflight-evidence .\evidence\preflight.observed.json `
  --observed-hardware-receipt .\evidence\hardware-runtime.observed.json `
  --hardware-receipt .\evidence\hardware-runtime.accepted.json `
  --output .\evidence\runtime.accepted.json
```

The runtime promotion validates the exact image, package, GPU and gateway
projections in the automated preflight and changes only the template status to
`accepted`; it never overwrites an existing receipt.

## 2. Populate or verify the sealed official model

The source is public and the operator has no token input. It downloads only the
14 code-owned root files from the exact revision; `metal/`, `original/`, nested
entries, links and any size/hash drift fail closed. Population refuses an
existing volume and performs a second offline verification before writing the
protected manifest copy.

Use the already-pinned SGLang image as the downloader runtime:

```powershell
$downloader = 'lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'

.\scripts\populate-model-volume.ps1 `
  -Mode Plan -DownloaderImage $downloader

.\scripts\populate-model-volume.ps1 `
  -Mode Populate -DownloaderImage $downloader `
  -OutputManifest .\evidence\source-model.verified.json -Apply

.\scripts\populate-model-volume.ps1 `
  -Mode Verify -DownloaderImage $downloader `
  -ManifestPath .\evidence\source-model.verified.json -Apply
```

The existing source volume must contain exactly two top-level entries:
`source-manifest.json` and `snapshot`. The manifest identity is fixed:

```text
raw sha256      438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9
semantic sha256 e75b176ed1817e762cf9b7f2262f6e58491a0f9d48d1ea51e466a6e2c3b8a3ab
files           14
bytes           13789264674
```

`model-manifest.example.json`, the manifest stored in the volume and the
protected `evidence/source-model.verified.json` copy are byte-identical. The
runtime does not trust the external copy in place of the volume: every container
start verifies the internal manifest and hashes every model file.

## 3. Provision independent gateway and engine credentials

`provision-secrets.ps1` is plan-only unless `-Apply` is supplied. It creates two
independent 256-bit bearer files plus a private P-256 CA/server certificate with
the laptop IP SAN, then restricts the ignored secret tree to the current Windows
SID and SYSTEM.

```powershell
.\scripts\provision-secrets.ps1
.\scripts\provision-secrets.ps1 -OpenSslPath C:\controlled\openssl.exe -Apply
```

Only the public `secrets/tls/ca.crt` may leave the laptop. Friday uses that CA
and the gateway bearer; the internal SGLang bearer never leaves the laptop.

## 4. Build an immutable candidate profile

Create `evidence/runtime.accepted.json` from `runtime-manifest.example.json`
only after preflight reproduces every pinned image, package and hardware value;
change only its status to `accepted`.

Rebuild the exact provisional finalist below. It remains a candidate until the
complete acceptance chain closes:

```powershell
python .\scripts\runtime_profile_operator.py candidate `
  --hardware-receipt .\evidence\hardware-runtime.accepted.json `
  --source-model-manifest .\evidence\source-model.verified.json `
  --runtime-manifest .\evidence\runtime.accepted.json `
  --ca-certificate .\secrets\tls\ca.crt `
  --sglang-compat-patch-sha256 ((Get-FileHash .\runtime\reasoner_grammar_backend.py -Algorithm SHA256).Hash.ToLowerInvariant()) `
  --sglang-sampler-compat-patch-sha256 ((Get-FileHash .\runtime\sampler.py -Algorithm SHA256).Hash.ToLowerInvariant()) `
  --context-tokens 4096 --max-output-tokens 512 `
  --mem-fraction-static 0.96 --chunked-prefill-size 256 `
  --kv-cache-dtype bf16 --page-size 1 `
  --radix-cache-enabled true --overlap-schedule-enabled true `
  --swa-full-tokens-ratio 0.80 --cuda-graph-backend-decode full `
  --allowed-modes assist,shadow --allowed-workloads extract `
  --profile-id-output .\evidence\profile.id `
  --output .\evidence\profile.candidate.json
```

The resulting profile ID must be
`gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`
and the exact candidate bytes must hash to
`51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439`.

`workload-policy.document-map.v1.json` is a Friday-side product rollout
manifest, not a replacement runtime profile. Its SHA-256 is
`7d57947d7ecda675e8a4da3f56332baf32484c08c0504afd7fa420b9c6323cd9` and it
binds the already accepted gateway manifest
`93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`.
Never point
`FRIDAY_SECONDARY_PROFILE_MANIFEST_PATH` at this policy file. The v1 policy is
shadow-only: enabling discarded document-map shadow changes neither file
mounted by Compose nor the running SGLang/gateway containers, so the laptop
needs no restart for that product-only transition. Assist requires a later
evidence-bound policy and operator gate; v1 must never be used to promote it.

The profile ID and served-model alias are derived from the complete engine
projection. `dtype=bfloat16`, `quantization=mxfp4`, global/prefill attention
`triton`, `moe_runner_backend=flashinfer_mxfp4`, hybrid SWA memory, one running
request, `mm_feature_transport=cpu`, prefill graphs disabled and no weight/KV
CPU offload are fixed. `deterministic_inference_enabled=false` is part of the
engine binding and the launcher never emits `--enable-deterministic-inference`.
SGLang `--language-only` is deliberately
not used: that is an encoder-disaggregation mode and rejects this GPT-OSS
architecture.

Optional candidate flags have these closed choices and defaults:

- `--kv-cache-dtype`: `bf16` (default) or `fp8_e4m3`; scale policy is derived as
  `not_applicable` or `implicit_unit` and cannot be supplied independently.
- Decode attention stays on `triton`; sampling remains fixed to the production
  `pytorch` path.
- `--page-size`: `1` (default) or `16`; `--radix-cache-enabled` and
  `--overlap-schedule-enabled`: `true` (default) or `false`.
- `--swa-full-tokens-ratio`: `0.25`, `0.50`, `0.80` (default) or `1.00`;
  `--chunked-prefill-size`: `256`, `512`, `1024` (default), `1536` or `2048`.
- `--cuda-graph-backend-decode`: `disabled` (default), which binds batch shape
  `0/[]`, or `full`, which is restricted to batch shape `1/[1]`.

`--context-tokens` must be one of
`4096,8192,12288,16384,24576,32768,40960,49152,65536`;
`--mem-fraction-static` must be one of
`0.86,0.88,0.90,0.92,0.94,0.95,0.96,0.97`. A candidate is only a candidate
until its exact evidence chain is promoted.

## 5. Configure and start the detached node

Copy `.env.example` to ignored `.env`. During certification, point
`FRIDAY_SECONDARY_PROFILE_MANIFEST_PATH` to the candidate. Keep the exact image
and source-volume values unchanged.

Audit/apply the firewall rule, then start only this detached bundle:

```powershell
.\scripts\test-firewall-classifier.ps1
.\scripts\firewall.ps1
.\scripts\firewall.ps1 -Apply
docker compose --env-file .env -f compose.yml config
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
```

Startup must prove the native source and the exact candidate projection. For the
finalist above that means BF16 KV, 4K, memory fraction 0.96, chunk 256, page one,
radix/overlap and hybrid SWA 0.80 enabled, full batch-one decode graph and
disabled prefill graph. Reject any NaN/Inf, repeated-token degeneration,
backend fallback, missing final channel, unexpected image identity or source
drift.

## 6. Probe, tune, soak and promote through TLS

All evidence tools bind the exact profile epoch, HTTPS endpoint and private CA,
and retain only bounded status, latency/token counts and hashes; they do not
retain prompts, responses, reasoning or secrets. Quality includes a
deterministic near-limit recall case generated from the candidate context, so an
FP8 candidate cannot pass on short prompts alone. Capacity evidence must match
the candidate context and memory exactly.

```bash
python scripts/probe_endpoint.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --output evidence/endpoint.observed.json

python scripts/quality_battery.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --output evidence/quality.observed.json
```

Run capacity schema v2 at the exact candidate context/memory. The accepted
protocol is non-streaming, seven repeats and exactly 512 completion tokens per
repeat; it stops at the first failed envelope, usage, context, headroom,
finish-reason or throughput gate. Run the fresh soak for at least 100 requests
and 30 minutes, then restart the runtime cold and repeat the identical v2
capacity command against the new process epoch:

```bash
python scripts/tune_context.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --candidates 4096 --repeats 7 --generation-tokens 512 \
  --mem-fraction-static 0.96 \
  --output evidence/context.initial.json

python scripts/soak.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --duration-sec 1800 --minimum-requests 100 \
  --output evidence/soak.observed.json

# After a cold runtime restart, repeat the exact v2 protocol.
python scripts/tune_context.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --candidates 4096 --repeats 7 --generation-tokens 512 \
  --mem-fraction-static 0.96 \
  --output evidence/context.cold-restart.json
```

Every capacity repeat carries a deterministic discriminator near the start of
the prompt. This forces a real near-limit prefill instead of measuring an
identical full radix-cache hit after the first request. Earlier passed soak and
streaming capacity-v1 receipts remain preserved as historical measurements,
but they cannot satisfy capacity-v2 acceptance.

The deterministic battery proves the mocked failure contract, but cannot claim
that the physical laptop disappeared. Run it and the controlled live outage
runner from the primary host:

```bash
python scripts/failure_battery.py \
  --candidate evidence/profile.candidate.json \
  --ca-file /secure/friday-secondary-ca.crt \
  --output evidence/failure.deterministic.json

python scripts/live_failure_battery.py controlled \
  --candidate evidence/profile.candidate.json \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --output evidence/failure.controlled-live.json
```

Physical laptop loss is a separate causal witness. Start it against the currently
running Friday backend, then run the code-owned request stage. Only when it prints
`request_submitted_power_off_laptop_now` physically power the laptop off. The
runner rejects a response that completed before the cut, proves transport loss
after the full authenticated request body was written, and invokes its bounded
primary health/continuity probe once. That probe is not represented as a Friday
scheduler fallback. Separately verify exactly one primary result for the mid-turn
and ordinary Friday requests, then record the off-state. Power the same laptop
back on and finish only after the exact candidate is healthy without restarting Friday:

```bash
primary_pid="$(systemctl --user show -p MainPID --value friday-backend.service)"

python scripts/live_failure_battery.py physical-begin \
  --candidate evidence/profile.candidate.json \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --primary-ca-file /secure/friday-primary-ca.crt \
  --primary-pid "$primary_pid" \
  --output evidence/failure.physical-begin.json

python scripts/live_failure_battery.py physical-causal-request \
  --candidate evidence/profile.candidate.json \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --primary-ca-file /secure/friday-primary-ca.crt \
  --state evidence/failure.physical-begin.json \
  --output evidence/failure.physical-causal.json

python scripts/live_failure_battery.py physical-off \
  --candidate evidence/profile.candidate.json \
  --ca-file /secure/friday-secondary-ca.crt \
  --primary-ca-file /secure/friday-primary-ca.crt \
  --state evidence/failure.physical-begin.json \
  --causal-state evidence/failure.physical-causal.json \
  --physical-power-loss-observed \
  --ordinary-primary-fallback-exactly-once-operator-observed \
  --mid-turn-primary-fallback-exactly-once-operator-observed \
  --no-effect-replay-operator-observed \
  --v12-readiness-unchanged-operator-observed \
  --output evidence/failure.physical-off.json

python scripts/live_failure_battery.py physical-finish \
  --candidate evidence/profile.candidate.json \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --primary-ca-file /secure/friday-primary-ca.crt \
  --state evidence/failure.physical-off.json \
  --readmitted-without-primary-restart-operator-observed \
  --output evidence/failure.physical-observed.json
```

`friday-primary-ca.crt` is the public CA or server certificate that validates
the fixed `https://127.0.0.1:8000` authority, including that IP in SAN. The
witness never uses `-k`, follows no redirect and does not accept the ambient
trust store. The causal receipt contains only request/body hashes, bounded byte
counts, process/CA identities and booleans. It retains neither prompt, response,
bearer nor exception text and sends neither tools nor effects. The manual
mid-turn fallback assertion remains a separate claim but cannot pass acceptance
without the causal transport receipt. Exact Friday scheduler deltas are proved
later by the product-linked assist-stage outage battery.

The physical witness above proves the node boundary only. Product evidence is a
separate automatic stage runner; manual requests plus counter-only sidecars are
rejected. Run that runner from the exact clean checkout at the active predecessor
commit. The sealed release copy is only its byte-for-byte trust anchor; never
execute the copied artifact standalone because its sibling imports and Git root
are part of the checked runtime boundary. At each rollout stage it creates one
bounded force-review Inbox item,
calls the authenticated admin advice route itself, checks the exact diagnostics
delta and persisted pending/no-Knowledge-Object state, then atomically hard-purges
that exact synthetic Raw/Inbox pair without review feedback or calibration writes.
The backend returns before/after diagnostics with the exact advice response and
binds them to the server-validated synthetic source hash; any unexpected counter
movement fails the closed stage oracle. Run it in an exclusive/quiescent window,
once after each public-shadow, private-shadow and assist activation:

```bash
primary_pid="$(systemctl --user show -p MainPID --value friday-backend.service)"

python scripts/live_failure_battery.py product-stage \
  --candidate evidence/profile.candidate.json \
  --ca-file /secure/friday-secondary-ca.crt \
  --primary-api-key-file /secure/friday-primary-api-key \
  --primary-ca-file /secure/friday-primary-ca.crt \
  --primary-pid "$primary_pid" \
  --stage public-shadow \
  --output evidence/product.public-shadow.json

# Repeat with --stage private-shadow after ENV2, and --stage assist after ENV3.
```

For the assist physical-loss cycle, run `--stage outage` immediately after the
laptop is physically off, then `--stage cooldown` while its circuit remains
open. After power-on run `--stage recovery`; that command waits only for the
bounded retry window and proves exact profile/model readmission. Use a fresh
create-only output path for every stage.

Each receipt binds request, result and storage hashes to its before/after
diagnostics. Public shadow must deny private Inbox text with zero endpoint
requests; private shadow must validate and discard exactly one secondary result;
assist and recovery must persist advice from the exact secondary alias; outage
and cooldown must each return one normal primary result. Receipts retain no
prompt, model response or bearer. Cleanup leaves no RawObject, Inbox item,
Knowledge Object, feedback, feedback state or searchable material. It retains
only a body-free idempotency tombstone under the exact synthetic cleanup token;
the store keeps at most one such replay receipt per stage (six total), prunes this
exact prefix after 24 hours, and applies the ordinary account-delete/export rules.
The primary API-key file must be a regular single-line
literal owner-token file with mode `0600` (or stricter); delegated administrators
are rejected even if their preset otherwise grants every capability.

For public/private rollout transitions the v2 receipt also carries a body-free,
server-HMAC attestation and a short-lived one-shot lookup capability. The immutable
release operator validates the sealed receipt, then calls the configured-owner-only
`POST /api/admin/secondary-product-witness/consume-rollout-attestation` over pinned
primary TLS before changing service/env state. The backend atomically burns the
attestation first: a lost consume response therefore fails closed, a retry is `409`,
and a fresh same-stage witness is required. Product material is purged immediately;
the body-free replay/consume tombstone remains bounded to one row per stage and 24h.

The witness checks TLS loss, laptop boot-epoch change, unchanged Friday process
epoch and exact candidate recovery. It never turns a service stop or mocked test
into a physical-loss claim. Combine the complete bound receipt chain, then create capacity
evidence and promote the same candidate while rechecking source, runtime,
hardware and CA:

```powershell
python .\scripts\runtime_profile_operator.py accept-capacity `
  --candidate .\evidence\profile.candidate.json `
  --initial-trial .\evidence\context.initial.json `
  --cold-restart-trial .\evidence\context.cold-restart.json `
  --soak .\evidence\soak.observed.json `
  --output .\evidence\capacity.accepted.json

python .\scripts\runtime_profile_operator.py accept-failure `
  --candidate .\evidence\profile.candidate.json `
  --deterministic .\evidence\failure.deterministic.json `
  --live .\evidence\failure.controlled-live.json `
  --physical-begin .\evidence\failure.physical-begin.json `
  --physical-causal-request .\evidence\failure.physical-causal.json `
  --physical-state .\evidence\failure.physical-off.json `
  --physical-observation .\evidence\failure.physical-observed.json `
  --output .\evidence\failure.accepted.json

python .\scripts\runtime_profile_operator.py accept-profile `
  --candidate .\evidence\profile.candidate.json `
  --hardware-receipt .\evidence\hardware-runtime.accepted.json `
  --source-model-manifest .\evidence\source-model.verified.json `
  --runtime-manifest .\evidence\runtime.accepted.json `
  --ca-certificate .\secrets\tls\ca.crt `
  --quality .\evidence\quality.observed.json `
  --capacity .\evidence\capacity.accepted.json `
  --soak .\evidence\soak.observed.json `
  --failure .\evidence\failure.accepted.json `
  --failure-deterministic .\evidence\failure.deterministic.json `
  --failure-live .\evidence\failure.controlled-live.json `
  --failure-physical-begin .\evidence\failure.physical-begin.json `
  --failure-physical-causal-request .\evidence\failure.physical-causal.json `
  --failure-physical-state .\evidence\failure.physical-off.json `
  --failure-physical-observation .\evidence\failure.physical-observed.json `
  --output .\evidence\profile.accepted.json
```

Only then register the exact accepted profile in Friday and deploy it
default-off. Real private product shadow is a separate
`secondary_shadow_to_private_shadow` activation that changes only the private
admission bit. After its evidence is accepted, `secondary_shadow_to_assist`
changes only the mode; a direct public-shadow-to-assist transition is rejected.
The primary model still performs final synthesis.

## Rollback

Disable the Friday secondary feature flag first; this immediately restores the
primary-only path. Then stop the laptop bundle without deleting evidence,
secrets, source volume or cache:

```powershell
docker compose --env-file .env -f compose.yml down
```

Do not substitute a mutable image, the rejected NVFP4 artifacts, CPU offload or
an unverified backend while claiming the accepted profile remains valid.
