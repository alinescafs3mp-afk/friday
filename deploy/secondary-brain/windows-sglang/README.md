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
network. The gateway disables access logs, exposes only health, models and chat
completions, and substitutes two local 256-bit hex secrets into a tmpfs config.
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
  -OutputPath .\evidence\preflight.observed.json
```

Review the report together with `wsl --version`, `wsl --status`, the Docker
Desktop WSL2 setting, AC sleep policy and Docker Desktop startup policy. The
script verifies the live Windows address, Linux-container engine, NVIDIA host
projection, CUDA allocation/kernel execution and presence of every baseline
SGLang flag, plus the exact gateway digest/platform/UID/version projection. It
hashes verbose WSL/help output instead of retaining it. The report remains
`inventory_incomplete` when any explicit container inspection is omitted.

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

## 5. Configure firewall and baseline Compose

`firewall.ps1` defaults to the only approved source, `192.168.1.78`, and TCP
8443. It audits exact-port and broad Docker allow rules after applying its own
rule. Resolve every reported conflict before rollout.

```powershell
.\scripts\firewall.ps1
.\scripts\firewall.ps1 -Apply
```

Copy `.env.example` to the ignored `.env`, replace the SGLang image placeholder
with its exact digest, set the verified external model volume, and leave the initial
4K/0.86 values as discovery values. Validate before starting:

```powershell
docker compose --env-file .env -f compose.yml config
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
```

This explicit command is the deployment boundary. None of the preparation
scripts invokes it. Before accepting the node, inspect the container/image
identities and startup logs to prove native ModelOpt FP4 loading, SM120
FlashInfer CUTLASS FP4 GEMM, FP8 KV, no CPU offload and no prompt/response-body
logging. If automatic quantization detection is not proven, test the exact
pinned image's explicit `--quantization modelopt_fp4` form in a separate
candidate and retain only the proven form.

## 6. Probe, tune and soak through TLS

All external probes use the gateway bearer and explicit private CA. They never
write raw prompts, responses or reasoning into evidence.

The container healthcheck uses certificate verification bypass only for its
same-process `127.0.0.1` liveness call because the pinned Alpine-slim BusyBox
`wget` has no custom-CA option. It is not an acceptance check: every host-side
probe below validates the private CA and the exact IP SAN.

```bash
python scripts/probe_endpoint.py \
  --base-url https://192.168.1.35:8443/v1 \
  --api-key-file /secure/friday-secondary-gateway-key \
  --ca-file /secure/friday-secondary-ca.crt \
  --output evidence/endpoint.observed.json
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
  --duration-sec 1800 --minimum-requests 100 \
  --output evidence/soak.observed.json
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
