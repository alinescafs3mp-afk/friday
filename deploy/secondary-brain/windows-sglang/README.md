# Optional Friday secondary brain: Windows + SGLang

This bundle prepares the inference-only node described by
`OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`. It does not
install a Friday backend and it is not part of the root Compose graph. Nothing
here is certified or started automatically.

The only LAN endpoint is the authenticated TLS gateway:

```text
Friday host 192.168.1.78
    -> https://192.168.1.35:8443/v1
    -> gateway bearer authentication and route allowlist
    -> internal Compose network
    -> http://sglang:30000 with a different SGLang bearer
```

TCP 30000 is never published. The SGLang service has only an internal Compose
network. The gateway disables access logs, exposes only health, models, chat
completions and authenticated operational metrics, and substitutes two local
256-bit hex secrets into a tmpfs config.
Neither secret belongs in `.env`, Docker labels, a command line, an image layer
or repository content.

## Invariants

- Model repository is exactly `shanjiaz/gpt-oss-20b-nvfp4-modelopt` at revision
  `fb9848e169d5b38cbc00ecf3383283ea1fc33a21`.
- SGLang must be a local immutable `repository@sha256:<64 lowercase hex>`
  reference. The gateway release provenance is the versioned tag
  `1.31.3-alpine3.24-slim`, but the deployment is pinned to its Nginx
  unprivileged OCI index
  `nginxinc/nginx-unprivileged@sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf`.
  For `linux/amd64` its platform manifest is
  `sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c`,
  config is `sha256:89dc7d054bddca245db3d5a779e363007d0e75b1161cfe2f283ebeaf0ed90d50`,
  and expected runtime user/version are `101`/`1.31.3`. Compose uses
  `pull_policy: never`; live inspection must confirm all of these facts.
- The example manifests are deliberately marked `template_not_accepted`. Do
  not convert placeholders into claims before live discovery.
- The model volume is mounted read-only for serving. No Docker socket,
  privileged mode, host network, CPU offload, speculative decoder or host model
  bind mount is present.
- The node has no Friday tools, storage, Telegram, V12 lease or effect authority.
- The primary Friday service must remain healthy with both containers stopped.
- The pinned SGLang build has no API-key-file option and logs its `ServerArgs`
  representation. `runtime/launch_sglang_secure.py` therefore reads the bearer
  from the mounted file, patches that exact pinned representation before parser
  construction, and invokes SGLang in-process; the secret is absent from OS
  argv/environment. The gateway renderer uses shell builtins only, so neither
  bearer enters a child argv. Admission must still scan complete container logs,
  process argv and process environments for both live values without printing
  them; any match rejects the node.

## 1. Establish key-based management

Run `install-openssh.ps1` in an elevated Windows PowerShell. Its default is a
read-only plan. Supply only a public key; never copy a private key or password
into this directory.

```powershell
.\scripts\install-openssh.ps1 `
  -UserName <local-user> `
  -PublicKeyFile C:\safe\friday-secondary.pub

.\scripts\install-openssh.ps1 `
  -UserName <local-user> `
  -PublicKeyFile C:\safe\friday-secondary.pub `
  -Apply
```

The resulting SSH firewall rule accepts only `192.168.1.78`. Verify a fresh SSH
session from that host before changing password authentication. The script
intentionally leaves password login unchanged so a failed key bootstrap cannot
lock out recovery. Rotate the bootstrap password out of band after key login is
proven; no credential is recorded by this bundle.

## 2. Pin live runtime identities and run preflight

Resolve the desired stable SGLang CUDA 13 image and a minimal CUDA/PyTorch
canary image on the laptop, inspect their local `RepoDigests`, and record exact
digests in a new accepted runtime manifest. A version tag may be examined only
during live discovery; it must never appear in `.env`, Compose input or an
accepted manifest. Pull the already pinned gateway reference separately and
confirm Nginx 1.31.3, `linux/amd64`, UID 101 and the platform/config digests
listed above.

The preflight is read-only unless `-RunGpuCanary` or `-InspectSglangHelp` is
explicitly supplied. Those switches create only ephemeral, network-disabled
containers and never pull images.

```powershell
.\scripts\preflight.ps1 `
  -CudaCanaryImage '<canary>@sha256:<digest>' `
  -SglangImage 'lmsysorg/sglang@sha256:<digest>' `
  -RunGpuCanary `
  -InspectSglangHelp `
  -InspectGatewayImage `
  -HardwareRuntimeReceiptOutputPath .\evidence\hardware-runtime.observed.json `
  -OutputPath .\evidence\preflight.observed.json
```

Review the report together with `wsl --version`, `wsl --status`, the Docker
Desktop WSL2 setting, AC sleep policy and Docker Desktop startup policy. The
script verifies the live Windows address, Linux-container engine, NVIDIA host
projection, CUDA allocation/kernel execution and presence of every baseline
SGLang flag, plus the exact gateway digest/platform/UID/version projection. It
also rejects drift from the measured Windows 11 build, WSL/kernel components,
Docker Desktop/Engine/Compose versions and the exact GPU UUID, name, VRAM,
compute capability and driver. Expected identities are code-owned; environment
variables cannot redefine them. It hashes verbose WSL/help output instead of
retaining it. The report remains `inventory_incomplete` when any explicit
container inspection is omitted.

The hardware/runtime receipt is canonical and `observed_unaccepted`. Review it,
then promote it without an editor (the first call is plan-only):

```powershell
.\scripts\accept-hardware-runtime-receipt.ps1 `
  -ObservedPath .\evidence\hardware-runtime.observed.json `
  -AcceptedPath .\evidence\hardware-runtime.accepted.json
.\scripts\accept-hardware-runtime-receipt.ps1 `
  -ObservedPath .\evidence\hardware-runtime.observed.json `
  -AcceptedPath .\evidence\hardware-runtime.accepted.json `
  -Apply
```

Promotion accepts only the one code-owned canonical observed byte sequence,
creates the accepted file exclusively (never overwrites), and reports the
closed accepted SHA-256
`0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f`.
Record it in the runtime profile as `hardware_runtime_receipt_sha256`. Configure
`FRIDAY_SECONDARY_HARDWARE_RUNTIME_RECEIPT_PATH` to that protected copy. The
receipt hash participates in the engine binding; before importing SGLang, the
launcher runs one five-second, single-row `/usr/bin/nvidia-smi` probe and
requires exact UUID/name/VRAM/compute-capability/driver equality. Any host,
runtime or profile drift stops only the optional node.

### 2a. Build the closed SGLang compatibility image

The pinned upstream image rejects the valid three-dimensional GPT-OSS expert
tensors before its ModelOpt loader can consume them and omits the model's
`quant_config` when constructing attention. `runtime-compat/` applies only
those two exact, hash-gated edits to the exact local base image. The build has
no network, package installation or remote source input:

```powershell
docker buildx build --load --pull=false --network=none --no-cache `
  --provenance=false --sbom=false --build-arg SOURCE_DATE_EPOCH=0 `
  --tag friday-secondary/sglang-compat:observed `
  .\runtime-compat
docker image inspect --format '{{.Id}}' friday-secondary/sglang-compat:observed
```

Repeat the build from the same committed context and require the identical
full image ID. The tag is only a local discovery handle; the runtime manifest
and deployment must use the measured immutable image ID. Acceptance still
requires native loader/backend evidence and the complete batteries below.

## 3. Discover and seal the exact model volume

`populate-model-volume.ps1` requires an exact, locally present downloader image
with `python3`, `huggingface_hub` and CA roots. It never pulls an image. `Plan`
is the default safe workflow; `Discover -Apply` creates a new volume, downloads
only the fixed revision, rejects symlinks, hashes every regular file and writes
an `observed_unaccepted` manifest. A Hugging Face token, if needed, is accepted
only as a read-only file mount and is never printed or placed in container
environment/arguments.

```powershell
$downloader = '<approved-downloader>@sha256:<digest>'
$volume = 'friday-secondary-gptoss20b-fb9848e-candidate'

.\scripts\populate-model-volume.ps1 -Mode Plan `
  -DownloaderImage $downloader -VolumeName $volume

.\scripts\populate-model-volume.ps1 -Mode Discover `
  -DownloaderImage $downloader -VolumeName $volume `
  -OutputManifest .\evidence\model.observed.json -Apply
```

Review the exact revision and hashes. Only after that review, copy the observed
manifest to an untracked accepted file and change `status` from
`observed_unaccepted` to `accepted`; do not edit file rows or aggregates. Seal
by verifying the read-only volume with networking disabled:

```powershell
.\scripts\populate-model-volume.ps1 -Mode Verify `
  -DownloaderImage $downloader -VolumeName $volume `
  -ManifestPath .\evidence\model.accepted.json -Apply
```

Retain the verification receipt. If download or verification fails, the script
keeps the candidate volume for inspection and never overwrites an existing one.

## 3a. Sealed internal ModelOpt conversion fallback

Use this path only for the supported internal fallback. Its source is exactly
`openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee`, stored root-only in
the existing `friday-secondary-source-gptoss20b` volume with its verified source
manifest. The operator never downloads, pulls, creates a volume, overwrites a
file, mounts a secret or enables container networking.

First generate the fixed synthetic corpus. It contains no Telegram, operator or
Friday production data and is content-addressed by the operator:

```powershell
py .\scripts\generate_calibration.py `
  --output .\evidence\modelopt-calibration.jsonl `
  --manifest .\evidence\modelopt-calibration.observed.json
```

Prepare a directory containing exactly the six pinned files listed in
`modelopt-converter-manifest.example.json`: the ModelOpt 0.45.0, Transformers
5.9.0 and Accelerate 1.12.0 wheels plus `hf_ptq.py`,
`cast_mxfp4_to_nvfp4.py` and `example_utils.py` from ModelOpt commit
`ec87a82927d003986d44fb7f4fa8b3d10c31b095`. Create the empty output volume
explicitly; conversion refuses an absent or non-empty volume:

```powershell
docker volume create friday-secondary-modelopt-conversion-output

$common = @{
  ArtifactDirectory = 'C:\ProgramData\FridaySecondary\converter\artifacts'
  CalibrationFile = '.\evidence\modelopt-calibration.jsonl'
  CalibrationManifest = '.\evidence\modelopt-calibration.observed.json'
}
.\scripts\convert-modelopt-nvfp4.ps1 -Mode Plan @common
.\scripts\convert-modelopt-nvfp4.ps1 -Mode Convert @common `
  -OutputManifest .\evidence\modelopt-conversion.observed.json -Apply
```

The preferred converter is the code-owned TRT-LLM `linux/amd64` child digest.
If that exact image is unavailable, the only alternative is a local image ID
bound by an explicitly accepted converter manifest. Start from
`modelopt-converter-manifest.example.json`; accept it only after proving the
exact SGLang base digest, Dockerfile/context and wheel hashes, package versions,
network-disabled build and a passing `pip check`, then add
`-AcceptedConverterManifest <path>` to every Plan/Convert/Verify invocation.
No tag or arbitrary image reference is accepted.

The conversion command is fixed to `nvfp4_mlp_only`, closed-form MXFP4-to-NVFP4
cast, unquantized KV cache, 256 synthetic samples at sequence length 512,
batch size 1, sequential device map, GPU memory fraction 0.70 and skipped
generation. Low-memory mode is deliberately absent. The container runs with
`--network none` and `--pull never`.

The produced manifest is `observed_unaccepted`. Accept it only after the exact
offline tensor and provenance audit passes. This acceptance seals model bytes;
it does not certify the runtime. Exact SGLang loader/backend, protocol, quality,
capacity and failure evidence later promote the separately bound runtime profile.
After the offline review, change only `status` to `accepted` in a protected copy
and seal the live volume:

```powershell
.\scripts\convert-modelopt-nvfp4.ps1 -Mode Verify @common `
  -AcceptedOutputManifest .\evidence\modelopt-conversion.accepted.json -Apply
```

Any partial output is retained for inspection and makes every later conversion
attempt fail closed; cleanup or retry is a separate, explicit operator action.

## 4. Create distinct auth secrets and a private IP-SAN CA

`provision-secrets.ps1` is the idempotent provisioning path. Its default is a
plan. With `-Apply` it uses the Windows cryptographic RNG for two independent
256-bit lowercase-hex bearers, uses a local OpenSSL executable to create a
P-256 private CA and server certificate, proves both key/certificate pairs and
the two IP SANs, then recursively restricts the ignored secret tree to the
current Windows SID and SYSTEM. It refuses partial or invalid existing state
and never emits bearer values, private keys or their hashes.

```powershell
.\scripts\provision-secrets.ps1
.\scripts\provision-secrets.ps1 -OpenSslPath C:\controlled\openssl.exe -Apply
```

A successful rerun verifies the same set without rotating it. All bearer and
private-key material remains on the laptop. The only artifact this step permits
to leave the laptop is `secrets/tls/ca.crt`; copy that public CA certificate to
the primary through the proven SSH channel. Later Friday integration must
inject the gateway bearer through its dedicated secret channel without placing
it in a report, command line or repository. The internal SGLang bearer never
leaves the laptop.

For the primary Docker runtime, place that public CA at the protected host path
`${FRIDAY_HOST_HOME}/data/secondary-brain/ca.crt` and set
`FRIDAY_SECONDARY_LLM_CA_FILE=/runtime/data/secondary-brain/ca.crt`. The existing
data mount exposes that exact container path. A Windows path or a path outside
`/runtime/data` is not visible inside the backend and leaves the optional client
safely misconfigured. The production URL is
`https://192.168.1.35:8443/v1`; Friday rejects plain LAN HTTP.

## 5. Build the candidate profile mechanically

Materialize `evidence/runtime.accepted.json` from the exact preflight/runtime
inspection: its schema/status must be
`friday.secondary-sglang-runtime.v1`/`accepted`, the image and 40-hex SGLang
revision must be exact, and `served_model_alias_policy` must remain
`friday-secondary-{profile_id}`. The operator rejects the template, wrong
hardware receipt, wrong source/conversion, mutable runtime, noncanonical profile
or an output path that already exists.

```powershell
python .\scripts\runtime_profile_operator.py candidate `
  --hardware-receipt .\evidence\hardware-runtime.accepted.json `
  --converted-model-manifest .\evidence\modelopt-conversion.accepted.json `
  --conversion-manifest .\evidence\modelopt-converter.accepted.json `
  --runtime-manifest .\evidence\runtime.accepted.json `
  --ca-certificate .\secrets\tls\ca.crt `
  --context-tokens 4096 --max-output-tokens 512 `
  --mem-fraction-static 0.86 --kv-cache-dtype none `
  --allowed-modes assist,shadow --allowed-workloads extract `
  --profile-id-output .\evidence\profile.id `
  --output .\evidence\profile.candidate.json
```

The profile ID and served alias are derived from the immutable engine
projection; they are never typed by hand. Evidence/status promotion later does
not change that engine identity.

## 6. Configure firewall and baseline Compose

`firewall.ps1` defaults to the only approved source, `192.168.1.78`, and TCP
8443. It audits exact-port and broad Docker allow rules after applying its own
rule. Resolve every reported conflict before rollout.

```powershell
.\scripts\firewall.ps1
.\scripts\firewall.ps1 -Apply
```

Copy `.env.example` to the ignored `.env`, replace the SGLang image placeholder
with its exact digest, set the verified external model volume, point
`FRIDAY_SECONDARY_PROFILE_MANIFEST_PATH` to the candidate during certification,
and leave the initial 4K/0.86 values as discovery values. After promotion, change
only that path to `profile.accepted.json`. Validate before starting:

```powershell
docker compose --env-file .env -f compose.yml config
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
```

This explicit command is the deployment boundary. None of the preparation
scripts invokes it. Before accepting the node, inspect the container/image
identities and startup logs to prove native ModelOpt FP4 loading, SM120
FlashInfer CUTLASS FP4 GEMM, FP8 KV, no CPU offload and no prompt/response-body
logging. Also prove that neither generated bearer occurs in either container's
complete logs, command line or environment. The gateway healthcheck expects the
secret-free unauthenticated `401`; SGLang's authenticated internal `/v1/models`
healthcheck asserts the exact served alias, so both service health states are
required. The accepted launcher uses explicit `--quantization modelopt_fp4`:
live auto-detection exposed ModelOpt metadata but left `quantization=None`, then
the GPT-OSS override selected an incompatible Triton MoE loader and rejected the
checkpoint's three-dimensional tensors. The explicit pinned method is therefore
a measured loader correction, not an online re-quantization request.

## 7. Probe, tune, soak and promote through TLS

All external probes use the gateway bearer and explicit private CA. They never
write raw prompts, responses or reasoning into evidence.

The gateway container healthcheck uses certificate verification bypass only for its
same-process `127.0.0.1` liveness call because the pinned Alpine-slim BusyBox
`wget` has no custom-CA option. It is not an acceptance check: every host-side
probe below validates the private CA and the exact IP SAN.

```bash
python scripts/probe_endpoint.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --output evidence/endpoint.observed.json
```

Run the full deterministic protocol and quality battery before capacity tuning.
It validates the exact model alias, Russian/English and structured responses,
reasoning modes, tool-call shape and continuation, multi-turn/context behavior,
Unicode, truncation and factual minimums. Stream cancellation is accepted only
when the bounded canary increments `sglang:num_aborted_requests_total` by exactly
one, both running/queued request gauges return to zero, and a fresh request
recovers inside the strict latency budget; merely completing eventually does not
pass. The battery never executes a requested tool and retains only closed status,
latency/token counts and output hashes.

```bash
python scripts/quality_battery.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --output evidence/quality.observed.json
```

Tune one explicitly configured context/memory candidate at a time. Update the
three measured values in `.env`, restart only this detached node, then run at
least three near-limit requests. Progress through 4K, 8K, 12K, 16K, 24K and
32K; repeat the winner after a cold container restart. The tuner reports a
trial, never an accepted capacity manifest.

```bash
python scripts/tune_context.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --candidates 4096 --mem-fraction-static 0.86 \
  --output evidence/context-4096-086.observed.json
```

Run the winning configuration for 30–60 minutes and at least 100 mixed
requests. The soak fails on any protocol/quality failure, less than 512 MiB or
5% free VRAM, or the configured thermal ceiling.

```bash
python scripts/soak.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --profile-manifest evidence/profile.candidate.json \
  --duration-sec 1800 --minimum-requests 100 \
  --output evidence/soak.observed.json
```

Repeat the winning capacity trial after a cold container restart. Then seal the
capacity and final profile; every evidence file must contain the exact candidate
profile ID/SHA, alias and CA hash. `failure.accepted.json` must cover the closed
15-journey laptop-off/disconnect/recovery set, exact-once primary fallback, no
effect replay and unchanged V12 readiness. Empty, cross-epoch, failed or partial
evidence is rejected. The two capacity receipts must expose different exact
`process_start_time_seconds` values; copying a trial cannot certify a restart.

Run the failure battery from the committed primary Friday checkout. It executes
the code-owned journey assertions in an isolated temporary `FRIDAY_HOME`, retains
no pytest output, and creates a new candidate-bound receipt only when every
mapped assertion passes:

```bash
python deploy/secondary-brain/windows-sglang/scripts/failure_battery.py \
  --candidate /protected/evidence/profile.candidate.json \
  --ca-file /protected/secondary-brain/ca.crt \
  --output /protected/evidence/failure.accepted.json
```

```powershell
python .\scripts\runtime_profile_operator.py accept-capacity `
  --candidate .\evidence\profile.candidate.json `
  --initial-trial .\evidence\context-winning.observed.json `
  --cold-restart-trial .\evidence\context-winning-cold.observed.json `
  --soak .\evidence\soak.observed.json `
  --output .\evidence\capacity.accepted.json

python .\scripts\runtime_profile_operator.py accept-profile `
  --candidate .\evidence\profile.candidate.json `
  --quality .\evidence\quality.observed.json `
  --capacity .\evidence\capacity.accepted.json `
  --soak .\evidence\soak.observed.json `
  --failure .\evidence\failure.accepted.json `
  --output .\evidence\profile.accepted.json
```

From `192.168.1.78`, prove: valid CA plus token succeeds; missing/wrong token is
401; `http://192.168.1.35:30000` is unreachable. From another LAN host, prove
TCP 8443 is blocked. Router forwarding, UPnP, public tunnels, RDP, WinRM and the
Docker API must remain closed.

## Rollback

Friday integration remains default-off and primary-only. Node rollback is:

```powershell
docker compose --env-file .env -f compose.yml down
```

No Friday database, primary model profile, storage or archive rollback is
required. Stopping the laptop must not change Friday readiness or ordinary
dialogue behavior.
