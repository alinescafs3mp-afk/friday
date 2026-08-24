# Optional Friday secondary brain: Windows + SGLang

This bundle runs one detachable, advisory GPT-OSS node on the Windows laptop at
`192.168.1.35`. Friday remains primary and final: the node has no tools, effects,
publication authority or V12 authority, and loss of the laptop must degrade to
the normal primary path.

The admitted runtime is one exact native-MXFP4 combination:

- model `openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee`;
- sealed volume `friday-secondary-source-gptoss20b`, mounted read-only at `/source`;
- model path `/source/snapshot` and manifest `/source/source-manifest.json`;
- SGLang `0.5.17` image
  `lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405`;
- image config
  `sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc`;
- SGLang source revision `29481685462732237d80d86076d6563e1f658102`;
- native `mxfp4` weights, `flashinfer_mxfp4` MoE on SM120, Triton attention,
  BF16 model/KV, one running request and CUDA graphs disabled.

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
- The runtime profile is canonical schema `friday.secondary-runtime-profile.v2`.
  It binds source, runtime image/config/OCI identities, backend selection,
  context, memory, graph policy and every acceptance receipt.
- The accepted-profile registry stays empty until protocol, quality, capacity,
  restart, soak and failure batteries all pass.

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
```

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

## 4. Build the immutable candidate profile

Create `evidence/runtime.accepted.json` from `runtime-manifest.example.json`
only after preflight reproduces every pinned image, package and hardware value;
change only its status to `accepted`.

Build a 4K, single-request, no-graph candidate:

```powershell
python .\scripts\runtime_profile_operator.py candidate `
  --hardware-receipt .\evidence\hardware-runtime.accepted.json `
  --source-model-manifest .\evidence\source-model.verified.json `
  --runtime-manifest .\evidence\runtime.accepted.json `
  --ca-certificate .\secrets\tls\ca.crt `
  --context-tokens 4096 --max-output-tokens 512 `
  --mem-fraction-static 0.97 --chunked-prefill-size 1024 `
  --allowed-modes assist,shadow --allowed-workloads extract `
  --profile-id-output .\evidence\profile.id `
  --output .\evidence\profile.candidate.json
```

The operator fixes `dtype=bfloat16`, `quantization=mxfp4`,
`kv_cache_dtype=bf16`, `attention_backend=triton`,
`moe_runner_backend=flashinfer_mxfp4`, `mxfp4_moe_precision=default` and both
CUDA-graph phases to `disabled`. The profile ID and served-model alias are
derived from the complete engine projection.

## 5. Configure and start the detached node

Copy `.env.example` to ignored `.env`. During certification, point
`FRIDAY_SECONDARY_PROFILE_MANIFEST_PATH` to the candidate. Keep the exact image
and source-volume values unchanged.

Audit/apply the firewall rule, then start only this detached bundle:

```powershell
.\scripts\firewall.ps1
.\scripts\firewall.ps1 -Apply
docker compose --env-file .env -f compose.yml config
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
```

Startup must prove the native source and runtime projection: `mxfp4`,
`flashinfer_mxfp4`, the SM120 FlashInfer CUTLASS path, BF16 KV, 4K token pool,
memory fraction 0.97, both graph phases disabled and no CPU offload. Reject any
NaN/Inf, repeated-token degeneration, backend fallback, missing final channel,
unexpected image identity or source drift.

## 6. Probe, tune, soak and promote through TLS

All evidence tools retain only bounded status, latency/token counts and hashes;
they do not retain prompts, responses, reasoning or secrets.

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

Run the capacity trial at the exact candidate context/memory, repeat it after a
cold runtime restart, then run at least 100 requests for at least 30 minutes:

```bash
python scripts/tune_context.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --candidates 4096 --mem-fraction-static 0.97 \
  --output evidence/context.initial.json

python scripts/soak.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --duration-sec 1800 --minimum-requests 100 \
  --output evidence/soak.observed.json
```

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

Physical laptop loss is a separate three-stage witness. Start it against the
currently running Friday backend, physically power the laptop off, exercise one
ordinary and one mid-turn Friday request and verify exactly one primary result
with no effect replay, then record the off-state. Power the same laptop back on
and finish only after the exact candidate is healthy and Friday has readmitted
it without restarting the primary process:

```bash
primary_pid="$(systemctl --user show -p MainPID --value friday-backend.service)"

python scripts/live_failure_battery.py physical-begin \
  --candidate evidence/profile.candidate.json \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --primary-pid "$primary_pid" \
  --output evidence/failure.physical-begin.json

python scripts/live_failure_battery.py physical-off \
  --candidate evidence/profile.candidate.json \
  --ca-file /secure/friday-secondary-ca.crt \
  --state evidence/failure.physical-begin.json \
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
  --state evidence/failure.physical-off.json \
  --readmitted-without-primary-restart-operator-observed \
  --output evidence/failure.physical-observed.json
```

The witness checks TLS loss, laptop boot-epoch change, unchanged Friday process
epoch and exact candidate recovery. It never turns a service stop or mocked test
into a physical-loss claim. Combine all four bound receipts, then create capacity
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
  --failure-physical-state .\evidence\failure.physical-off.json `
  --failure-physical-observation .\evidence\failure.physical-observed.json `
  --output .\evidence\profile.accepted.json
```

Only then register the exact accepted profile in Friday, deploy default-off,
observe shadow mode, and enable the narrow assist workload. The primary model
still performs final synthesis.

## Rollback

Disable the Friday secondary feature flag first; this immediately restores the
primary-only path. Then stop the laptop bundle without deleting evidence,
secrets, source volume or cache:

```powershell
docker compose --env-file .env -f compose.yml down
```

Do not substitute a mutable image, the rejected NVFP4 artifacts, CPU offload or
an unverified backend while claiming the accepted profile remains valid.
