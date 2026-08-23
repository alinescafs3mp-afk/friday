# Optional Secondary Brain: SGLang + GPT-OSS-20B NVFP4 Architect Implementation Brief

> Document ID: FRIDAY-SECONDARY-LLM-001  
> Status: external architecture and implementation handoff, draft v0.1  
> Date: 24 August 2026  
> Observed Friday repository checkpoint: `main` at `ef00a457b0b203febd2eab494b63f605353c35b7`  
> Target optional node: Windows laptop, Docker Desktop with WSL2, static LAN address `192.168.1.35`  
> Target accelerator: NVIDIA GeForce RTX 5080 Laptop GPU, expected 16 GB VRAM, but every hardware fact must be measured live  
> Selected model candidate: `shanjiaz/gpt-oss-20b-nvfp4-modelopt`  
> Required model revision: `fb9848e169d5b38cbc00ecf3383283ea1fc33a21`  
> Intended role: optional text reasoning, critique, classification, extraction and other advisory workloads  
> Non-negotiable invariant: Friday must remain fully usable on its existing primary 27B model when the laptop is off, asleep, unreachable, busy, restarting or broken

## How to use this brief

This is an implementation order, not a request for a speculative design document.

Before editing code:

1. Read the current Friday canonical status and architecture documents.
2. Re-resolve the current source commit and current live release. The checkpoint above is only the state observed while this brief was prepared.
3. Inspect all current `LLMRouter`, V12 model-gate, startup, diagnostics, worker and Compose construction sites.
4. Verify the laptop hardware, Windows build, NVIDIA driver, WSL2 kernel and Docker Desktop GPU path live.
5. Bring up and certify the selected model endpoint independently of Friday.
6. Implement the secondary endpoint behind a default-off feature flag.
7. Prove the laptop-off and laptop-disappears-mid-turn journeys before any output from the secondary model may influence a user-visible answer.
8. Roll out through shadow and advisory stages. Do not grant the secondary model tool or effect authority in this package.

The operator has supplied remote access details separately. They are operational secrets, not repository configuration. Do not write the Windows password, an HF token, the SGLang API key, a private SSH key or a copied credential into source, documentation, commits, Docker image layers, command transcripts, test fixtures, logs or evidence bundles.

## Operator objective

Add one optional model node to Friday:

```text
primary Friday host
    existing Qwen3.8 27B SGLang endpoint
    existing backend, storage, tools, V12 authority and user-facing runtime

optional laptop node at 192.168.1.35
    Docker Desktop, WSL2 GPU-PV
    one SGLang container
    one GPT-OSS-20B ModelOpt NVFP4 checkpoint
    OpenAI-compatible endpoint
```

Desired behavior:

```text
secondary healthy and admitted
    -> Friday uses it only for explicitly eligible work
    -> the primary model remains final authority
    -> useful work is offloaded or an independent advisory opinion is added

secondary absent, unhealthy, saturated or rejected
    -> no startup outage
    -> no failed user turn solely because of the secondary
    -> no long wait for it
    -> no repeated mutating action
    -> Friday proceeds on the existing primary 27B path
```

The laptop is a detachable accelerator, not a new single point of failure.

## Executive architecture decision

Build two independent endpoint clients under one code-owned scheduler:

```text
                           Friday control plane
                                  |
                    +-------------+-------------+
                    |                           |
             primary endpoint              optional endpoint
         Qwen3.8 27B / current host     GPT-OSS-20B / laptop
                    |                           |
        required for normal service       never required for boot
        dialogue and final synthesis      advisory and bounded work
        tools and effect decisions        no direct tools or effects
        current V12 authority             no inherited V12 authority
```

Do not put a load-balancing proxy in front of the two models. A network proxy cannot know whether a request is dialogue, deterministic classification, a read-only critique, a tool-selection step or a potentially effectful plan. It would also blur the exact endpoint and served-model binding already enforced by the V12 path.

Do not turn the laptop into a second Friday backend. It must host inference only. There is one canonical Friday storage, one authorization boundary, one tool kernel, one Telegram bridge and one publisher of user-visible results.

Do not replace the current `FRIDAY_LLM_BASE_URL` semantics. Preserve the primary endpoint as the default and add a distinct optional secondary configuration surface.

## Selected Hugging Face checkpoint

Use:

```text
repository: shanjiaz/gpt-oss-20b-nvfp4-modelopt
revision:   fb9848e169d5b38cbc00ecf3383283ea1fc33a21
format:     Safetensors, NVIDIA ModelOpt-style offline NVFP4
size:       approximately 13.5 GB across three weight shards
family:     GptOssForCausalLM
```

The repository contains:

```text
model-00001-of-00003.safetensors    approximately 4.88 GB
model-00002-of-00003.safetensors    approximately 4.90 GB
model-00003-of-00003.safetensors    approximately 3.66 GB
config.json
hf_quant_config.json
chat_template.jinja
tokenizer files
```

Its `hf_quant_config.json` declares:

```json
{
  "producer": {
    "name": "modelopt",
    "version": "0.37.0.dev56+g26c203abd"
  },
  "quantization": {
    "quant_algo": "NVFP4",
    "kv_cache_quant_algo": "FP8",
    "group_size": 16,
    "exclude_modules": ["lm_head"]
  }
}
```

This is the best currently found fit for the requested combination because it is a compact, packed ModelOpt checkpoint rather than a 40 GB fake/dequantized export, a GGUF file intended for llama.cpp, or a custom format with no SGLang loader.

### Candidate status and trust

Treat this checkpoint as an untrusted candidate until the live battery passes.

Reasons:

- it has no model card;
- it has very limited public validation;
- it was produced by a development build of ModelOpt 0.37;
- SGLang's first-party GPT-OSS examples primarily exercise the official MXFP4 model, not this exact community NVFP4 artifact;
- historical SM120 NVFP4 bugs include NaN output, corrupted batched decode and combinations that fail under speculative decoding.

The repository revision must be pinned. Never download floating `main` in production. Record the resolved commit and SHA-256 of every downloaded file in a local deployment manifest.

### Rejected alternatives

Do not silently substitute these:

- `shanjiaz/gpt-oss-20b-nvfp4`: approximately 40.9 GB, unsuitable for a 16 GB GPU.
- `FreedomAISVR/gpt-oss-20B-NVFP4-GGUF`: compact, but GGUF and aimed at llama.cpp rather than the requested native ModelOpt SGLang path.
- `narendra747/gpt-oss-20b-nvfp4`: custom NML artifact, not a SGLang ModelOpt checkpoint.
- `2imi9/gpt-oss-20B-NVFP4A16-BF16`, `arathishree/gpt-oss-20b-NVFP4` and other weakly documented exports: do not admit without independently proving packed size, loader path, tensor semantics and quality.

If the selected checkpoint cannot pass the battery, stop the rollout. The supported fallback is to produce an internal, revision-pinned ModelOpt NVFP4 conversion from a known source checkpoint using a current tested ModelOpt release, then rerun the same battery. Do not fall back to CPU offload or a random HF quant while claiming the requested node is complete.

## Model and hardware implications

GPT-OSS-20B is a text Mixture-of-Experts model. Its architecture has approximately 21B total parameters but only a small subset active per token. The selected config uses 24 transformer layers, 64 query heads, 8 KV heads, head dimension 64, alternating sliding and full attention, and a nominal 131,072-token model limit.

The primary Qwen3.8 27B is a different model family and remains better aligned with Friday's established prompts, Russian-language behavior, multimodal path and current tool/runtime certification. The GPT-OSS node therefore adds model diversity and spare throughput. It is not a smaller clone of the primary brain.

The GPT-OSS endpoint must use its Harmony-aware chat template and SGLang's GPT-OSS reasoning and tool parsers. Do not apply Qwen-specific response assumptions to it.

### Memory estimate

The checkpoint occupies approximately 13.5 GB on disk. Actual device allocation is larger than file size because loading, scales, allocator state, CUDA graphs, kernels, activations and temporary workspaces also consume VRAM.

For a simple upper-bound estimate, raw FP8 KV storage for full attention is approximately:

```text
24 layers
x 2 tensors, K and V
x 8 KV heads
x 64 values per head
x 1 byte per FP8 value
= 24,576 bytes per token
```

Illustrative raw KV sizes:

```text
4K tokens     about 96 MiB
8K tokens     about 192 MiB
16K tokens    about 384 MiB
32K tokens    about 768 MiB
```

This is not an admission calculation. Alternating sliding attention, page allocation, SGLang pool sizing and runtime workspaces change the real result. Measure it.

The optimization order is:

1. correct native NVFP4 execution;
2. stable full-GPU residency with no hidden CPU offload or WSL paging;
3. one reliable request;
4. maximum proven context with safety headroom;
5. low-memory SGLang accelerations;
6. additional concurrency, only if memory remains.

Do not trade correctness or context for a decorative collection of flags.

## Laptop preflight

### Remote access

An IP address and account password are not themselves a remote management channel.

Before automated work, verify that one controlled channel is available:

- Windows OpenSSH Server with key-based authentication, preferred;
- or an already established remote execution facility explicitly approved by the operator.

Use the supplied account only as an out-of-band bootstrap credential. Install an SSH public key, verify key login, then disable password login if this does not break the operator's recovery path. Use a dedicated non-administrator service account where practical. Rotate the bootstrap password after setup.

Do not expose SSH, RDP, WinRM, Docker API or SGLang to the public internet.

### Required Windows and Docker state

Prove all of the following:

```text
Windows edition/build is supported by current Docker Desktop
Docker Desktop uses the WSL2 backend
wsl --version reports a current WSL release
wsl --update succeeds or reports current
current NVIDIA Windows driver supports WSL2 GPU-PV
Docker Desktop is using Linux containers
Docker Desktop starts automatically for the intended Windows session
the laptop remains awake while connected to AC power
the static address is still 192.168.1.35
```

Run a GPU container canary before SGLang. The canary must prove:

```text
nvidia-smi works inside a Linux container
the expected GPU is visible
reported VRAM is recorded
torch.cuda.get_device_capability() is recorded
CUDA can allocate and execute a kernel
```

Do not assume the marketing label proves SM120 or exactly 16 GB. Store the measured facts in the deployment evidence.

### Storage

Prefer a Docker named volume or WSL2 ext4-backed path for model files and cache. NTFS bind mounts through the Windows-to-WSL boundary are acceptable only after measuring load time and stability.

Download the exact revision once, verify it, then run the serving container offline with respect to Hugging Face where practical:

```text
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

The model directory must be mounted read-only into the serving container.

## SGLang image selection

Start with the current immutable stable release line, presently SGLang `v0.5.16`, using its CUDA 13 image suitable for Blackwell. Use a runtime image if it contains every required kernel.

Do not use mutable tags such as:

```text
latest
dev
dev-cu13
```

A mutable tag may be used only during discovery. The accepted deployment must pin a full image digest.

If stable `v0.5.16` cannot load this exact checkpoint correctly and a current nightly contains a relevant fix, test one immutable dated nightly tag and pin its digest. Record:

```text
SGLang git revision
SGLang version
container image digest
CUDA runtime version
PyTorch version
FlashInfer version
sgl-kernel version
NVIDIA driver version
GPU name, VRAM and compute capability
checkpoint repository and revision
all launch arguments
```

Do not move to a nightly merely because it is newer. Require a measured reason and rerun the entire battery.

## SGLang launch strategy

### Baseline first

The first boot is a correctness baseline, not a benchmark victory lap.

Required baseline characteristics:

```text
single GPU
tensor parallel size 1
maximum running requests 1
no CPU offload
no speculative decoder
no NVFP4 KV cache
FP8 KV cache from the checkpoint, if the pinned SGLang build accepts it correctly
Triton attention for the initial GPT-OSS/SM120 baseline
FlashInfer CUTLASS FP4 GEMM for native SM120 NVFP4
chunked prefill
GPT-OSS reasoning parser
GPT-OSS tool-call parser
metrics and cache reporting
radix cache enabled
overlap scheduler enabled
```

SGLang's current quantization guidance says an offline pre-quantized checkpoint should normally be auto-detected without adding a redundant online quantization flag. This checkpoint also carries ModelOpt metadata. Test auto-detection first and inspect startup logs.

The accepted startup must prove that the loader selected the native ModelOpt FP4 path. If it does not, test the explicit form:

```text
--quantization modelopt_fp4
```

Pin the one form that provably selects the correct loader on the chosen image. Do not keep both variants or infer success from HTTP health alone.

### Candidate command shape

The architect must generate the exact final command from the pinned image's `--help`. The expected shape is:

```bash
python3 -m sglang.launch_server \
  --model-path /models/gpt-oss-20b-nvfp4-modelopt \
  --served-model-name friday-secondary-gptoss20b \
  --host 0.0.0.0 \
  --port 30000 \
  --api-key <injected-secret> \
  --reasoning-parser gpt-oss \
  --tool-call-parser gpt-oss \
  --attention-backend triton \
  --fp4-gemm-backend flashinfer_cutlass \
  --kv-cache-dtype fp8_e4m3 \
  --chunked-prefill-size 1024 \
  --max-running-requests 1 \
  --cuda-graph-max-bs 1 \
  --enable-metrics \
  --enable-cache-report
```

Notes:

- verify the exact spelling of every argument against the pinned image;
- do not put the real API key in a committed Compose command;
- omit `--kv-cache-dtype` if the pinned build correctly consumes the checkpoint's FP8 KV declaration and the explicit value causes a mismatch;
- add `--quantization modelopt_fp4` only if the accepted loader evidence requires it;
- tune `--mem-fraction-static`, `--context-length` and `--max-total-tokens` through the capacity procedure below;
- leave the radix cache and overlap scheduler enabled by not passing their disable flags.

### Feature policy

Enable after individual proof:

- native ModelOpt NVFP4;
- FlashInfer CUTLASS FP4 GEMM on SM120;
- FP8 KV cache;
- chunked prefill;
- radix prefix cache;
- overlap scheduler;
- CUDA graph capture for batch size 1;
- FlashInfer sampling if the pinned build selects it cleanly;
- xgrammar structured-output support;
- reasoning parsing;
- tool-call parsing;
- Prometheus-style metrics;
- cache-report telemetry;
- bounded request statistics that contain no prompt text.

Keep disabled in the first release:

- speculative decoding, including EAGLE, NEXTN, MTP, DSPARK and DFLASH;
- NVFP4 KV cache;
- batch size or concurrency above 1;
- expert parallelism and DeepEP;
- CPU weight offload;
- hierarchical cache to system RAM or disk;
- built-in Exa web search;
- SGLang Python execution or demo tool server;
- external MCP tool servers;
- automatic context truncation;
- request or response body logging;
- arbitrary remote code unless the exact checkpoint requires and the code is reviewed.

Reasons for excluding speculative decoding and NVFP4 KV are concrete: current SM120 reports include crashes or corrupted output for some NVFP4 KV and speculative combinations. The laptop also has no memory budget for a second draft checkpoint. "Maximum features" means the maximum proven set that preserves correctness and useful context, not the maximum number of command-line switches.

### NaN and degeneration guard

Certification runs should enable SGLang's NaN detection if supported by the pinned build. Production may disable the expensive detector only after the soak is clean, but Friday's existing repeated-token degeneration guard must remain active for secondary output.

Reject the endpoint if any case produces:

```text
NaN or Inf logits
empty final content without a valid expected protocol outcome
garbled token streams
unbounded repeated-token loops
invalid UTF-8
unparseable Harmony channels
reasoning text leaked into final content
wrong served-model alias
```

## Context capacity procedure

Do not guess a final context length from the nominal 131K model limit.

Create an automated tuner that:

1. starts with a 4,096-token admitted request envelope;
2. performs a real near-limit prefill plus at least 256 generated tokens;
3. records SGLang pool size, startup allocation, peak VRAM, free VRAM after graph capture, TTFT, output rate and errors;
4. repeats each candidate at least three times;
5. advances through `8K`, `12K`, `16K`, `24K`, `32K`, then larger only if still healthy;
6. repeats the winning size after a cold container restart;
7. runs the winning size during the thermal soak;
8. keeps at least 512 MiB absolute VRAM headroom and preferably 5 percent, whichever is larger;
9. rejects any configuration that uses CPU offload, Windows shared-memory spill, severe paging or unstable first-request allocation;
10. emits one immutable capacity manifest consumed by Friday's secondary profile.

Tune `mem-fraction-static` as a measured grid, for example:

```text
0.86
0.88
0.90
0.92
```

A higher value is not automatically better. It can starve activations or CUDA graph capture. The result is the largest stable admitted context, not the largest value that lets `/health` return 200.

If CUDA graphs consume enough memory to reduce useful context materially, benchmark both choices. Preserve the operator's preference for context unless graphs produce a substantial measured latency gain on the real single-request workload.

## Laptop deployment bundle

Create a separate deployment bundle. Do not add a second Friday backend to the root Compose file.

Suggested layout:

```text
deploy/secondary-brain/windows-sglang/
    README.md
    compose.yml
    .env.example
    model-manifest.example.json
    scripts/
        preflight.ps1
        install-openssh.ps1
        populate-model-volume.ps1
        probe_endpoint.py
        tune_context.py
        soak.py
        firewall.ps1
```

The final Compose service should have:

```text
restart: unless-stopped
GPU device reservation
IPC and shared-memory settings required by the pinned image
read-only model mount
persistent cache volume
no Docker socket mount
no host filesystem mount beyond the dedicated deployment roots
no privileged mode
no host network mode
a healthcheck
port 30000 only
request-body logging disabled
```

A conceptual Compose fragment:

```yaml
services:
  friday-secondary-gptoss:
    image: lmsysorg/sglang@sha256:<accepted-digest>
    container_name: friday-secondary-gptoss
    restart: unless-stopped
    ipc: host
    shm_size: "8gb"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    ports:
      - "30000:30000"
    environment:
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
    volumes:
      - friday-secondary-models:/models:ro
      - friday-secondary-cache:/root/.cache
      - type: bind
        source: ./secrets
        target: /run/friday-secrets
        read_only: true
    command:
      - /bin/bash
      - /opt/friday/start-secondary.sh
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:30000/health"]
      interval: 15s
      timeout: 3s
      retries: 10
      start_period: 600s

volumes:
  friday-secondary-models:
  friday-secondary-cache:
```

The real bundle must use the exact syntax supported by the installed Docker Compose version. A green HTTP healthcheck is necessary but not sufficient. The separate probe must verify `/v1/models` and perform a bounded generation.

### LAN exposure

Bind the service to the laptop LAN interface through Docker Desktop and protect it twice:

1. a strong random SGLang API key;
2. Windows Firewall allowing TCP 30000 only from the primary Friday host address.

Do not allow the whole LAN if the primary host has a stable address. Verify from:

```text
primary Friday host: request succeeds with token
primary Friday host: request fails without token
unapproved LAN host: connection is blocked
laptop itself: health and canary succeed
```

Never expose port 30000 through router port forwarding, a public tunnel or UPnP.

## Friday configuration contract

Add a separate optional namespace. Suggested variables:

```env
FRIDAY_SECONDARY_LLM_ENABLED=0
FRIDAY_SECONDARY_LLM_MODE=shadow
FRIDAY_SECONDARY_LLM_BASE_URL=http://192.168.1.35:30000/v1
FRIDAY_SECONDARY_LLM_MODEL=friday-secondary-gptoss20b
FRIDAY_SECONDARY_LLM_API_KEY=
FRIDAY_SECONDARY_LLM_CONNECT_TIMEOUT_SEC=1.0
FRIDAY_SECONDARY_LLM_READ_TIMEOUT_SEC=12.0
FRIDAY_SECONDARY_LLM_CALL_BUDGET_SEC=15.0
FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10
FRIDAY_SECONDARY_LLM_HEALTH_INTERVAL_SEC=30
FRIDAY_SECONDARY_LLM_COOLDOWN_SEC=60
FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS=<measured>
FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY=1
FRIDAY_SECONDARY_LLM_WORKLOADS=classify,extract,query_rewrite,summarize,critique,verify
FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1
```

Allowed modes should be a closed vocabulary:

```text
disabled
shadow
assist
```

Unknown values must fail closed to `disabled`, or stop startup with a clear configuration error if that matches the existing Friday configuration policy. They must never silently select `assist`.

Semantics:

- `disabled`: no client, probe or traffic.
- `shadow`: eligible calls may be copied after the primary path is already guaranteed; results are evaluated and discarded.
- `assist`: admitted workloads may use secondary output under the routing and fallback contracts below.

The current primary variables remain authoritative and unchanged.

Add the new variables to direct launch, Docker Compose, `.env.example`, diagnostics and secret scanning. Never make them required substitutions in Compose. A missing secondary URL or token must not prevent the backend container from starting.

## Client refactor

Current `LLMRouter` combines endpoint configuration, OpenAI-compatible transport, concurrency, retry, timeout and circuit-breaker state for one endpoint.

Refactor with minimal blast radius:

```text
LLMEndpointConfig
    immutable endpoint URL
    served model alias
    API key
    protocol family
    timeout and concurrency limits
    context limit

LLMEndpointClient
    one transport and one health/circuit state per endpoint
    current payload fitting and response validation
    endpoint-specific protocol adapter

ModelPool / SecondaryBrainScheduler
    primary client
    optional secondary client
    workload admission
    fallback policy
    telemetry
```

An acceptable lower-risk intermediate implementation is to keep the class name `LLMRouter` and instantiate it twice, provided endpoint settings and state are truly independent. Do not share a circuit breaker, semaphore, model alias or silent cooldown between machines.

Preserve all existing primary behavior and tests first. The primary construction must still work when no secondary fields exist.

## GPT-OSS protocol adapter

Create a code-owned model protocol profile, separate from authority:

```text
family: gpt_oss
chat protocol: Harmony through the checkpoint chat template
reasoning field: reasoning_content
reasoning policy: consume for protocol validation, never publish or persist raw
tool parser: gpt-oss
native tools: probeable, but denied for secondary execution in this package
vision: false
structured output: allowed after probe
exact response model: friday-secondary-gptoss20b
```

The adapter must:

- merge/system-normalize only in a way compatible with GPT-OSS Harmony;
- preserve valid assistant tool-call and tool-result pairing when used in probes;
- accept SGLang's OpenAI-compatible response shape;
- separate final content from `reasoning_content`;
- strip any Harmony service markers that leak into content;
- reject wrong model aliases;
- retain bounded usage and latency metadata;
- never log raw reasoning;
- never store raw reasoning in messages, audit rows, traces or failure objects;
- return a typed sanitized advisory result.

Do not reuse Qwen-specific parser names or assume Qwen thinking tags.

## Workload routing

Routing is code-owned and explicit. The model never chooses which endpoint receives the next task.

Define a closed workload enum, for example:

```python
class ModelWorkload(StrEnum):
    DIALOGUE = "dialogue"
    FINAL_SYNTHESIS = "final_synthesis"
    TOOL_CONTROL = "tool_control"
    EFFECT_PLANNING = "effect_planning"
    VISION = "vision"

    CLASSIFY = "classify"
    EXTRACT = "extract"
    QUERY_REWRITE = "query_rewrite"
    SUMMARIZE = "summarize"
    DOCUMENT_MAP = "document_map"
    CRITIQUE = "critique"
    VERIFY = "verify"
    PLAN_CANDIDATE = "plan_candidate"
```

Initial ownership:

| Workload | Preferred endpoint | Failure behavior |
|---|---|---|
| dialogue | primary | existing primary behavior |
| final synthesis | primary | existing primary behavior |
| tool control | primary | never delegated |
| effect planning or execution | primary | never delegated |
| vision or image-bearing prompt | primary | never delegated |
| classify | secondary when admitted | primary fallback |
| extract | secondary when admitted | primary fallback |
| query rewrite | secondary when admitted | primary fallback |
| text-only summarize | secondary when admitted | primary fallback |
| text-only document-map leaf | secondary when admitted | primary fallback |
| critique | secondary optional | skip on failure |
| verify | secondary optional initially | skip or use existing primary policy |
| plan candidate | secondary optional | discard on failure |

Do not route based only on `foreground` versus `background`. Priority and model role are different axes.

### Advisory influence boundary

Secondary output is an untrusted suggestion.

It may:

- produce a typed classification;
- propose extracted fields;
- suggest a query rewrite;
- summarize a bounded text;
- critique a draft;
- propose a read-only plan candidate.

It may not:

- invoke a Friday tool;
- authorize a capability;
- execute code;
- browse through SGLang's built-in tools;
- mutate storage;
- approve Inbox promotion;
- publish a final answer by itself;
- create a reminder, mission or external effect;
- change tenant or uploader authority;
- inherit the primary V12 lease.

When advisory text is inserted into a primary prompt, label it explicitly as untrusted secondary advice and apply the same prompt-injection hygiene used for retrieved material.

## Fail-soft availability contract

The secondary is usable only when all of these are true:

```text
feature enabled
configuration complete
circuit not open
local admission slot immediately available
recent endpoint probe healthy
/v1/models contains the exact alias
bounded generation canary passed for the current process epoch
requested context fits the measured secondary profile
workload is allowlisted
prompt contains no unsupported image payload
data policy permits transmission to the laptop
```

### State machine

Use a small process-local state machine:

```text
disabled
probing
healthy
degraded
cooldown
```

Transitions must be observable but content-free.

Startup rules:

- primary startup and readiness are unchanged;
- secondary failure never fails backend startup;
- do not wait through the full secondary model-load window before declaring Friday ready;
- diagnostics may report `optional_secondary_unavailable`;
- one bounded probe may run after startup;
- no noisy probe loop while the laptop is known absent.

Request rules:

- acquire the secondary slot with a tiny admission budget;
- if busy, skip or use primary rather than queueing a person behind the optional node;
- connection refusal, timeout, HTTP 5xx, 429, malformed JSON, wrong model alias, bad protocol, NaN/degeneration or cancellation opens an appropriate bounded cooldown;
- after cooldown, admit one half-open probe;
- a successful real call may serve as the health signal;
- never retry the same silent secondary generation three times inside a user turn.

### Fallback classes

Implement two explicit call forms:

```text
secondary_preferred_required_result
    secondary success -> use typed result
    secondary failure -> make the required call on primary

secondary_optional_advice
    secondary success -> use typed advisory result
    secondary failure -> continue without it
```

Do not hide these semantics behind one generic retry method.

### Side-effect invariant

A secondary failure must never trigger a replay of a mutating primary step.

The secondary participates only before effects or after effects as read-only observation. Existing idempotency and uncertain-effect fences remain authoritative.

## Foreground use without making the laptop mandatory

The first useful foreground pattern should be bounded critique, not round-robin dialogue.

For selected complex, read-only turns:

```text
retrieve and prepare evidence
        |
primary produces draft or bounded plan
        |
secondary receives draft + bounded evidence and returns structured critique
        |
primary performs final synthesis
```

The critique call gets a small absolute deadline. If it misses the deadline, cancel it, mark `secondary_skipped=deadline`, and publish through the primary path.

A later optimization may start an advisory plan on the secondary in parallel with deterministic retrieval. Do not create an unbounded sequential "debate" loop.

Simple chat should remain one primary call. Calling two models on every greeting wastes heat and adds no useful resilience.

## Background and document use

The best early throughput wins are:

- Inbox advisory classification;
- structured extraction of people, dates, amounts and document attributes;
- query rewriting for archive search;
- bounded summaries;
- text-only document-map leaves;
- independent answer critique.

For every migrated call site:

1. define its required output schema;
2. define whether failure means primary fallback or skip;
3. preserve current tenant and uploader boundaries;
4. cap input chars/tokens and output tokens;
5. add a deterministic validator;
6. compare shadow quality before enabling assist mode;
7. retain the original primary implementation as fallback.

Never send image payloads to the text-only secondary. OCR or extracted text may be sent only after the existing authorization and private-lineage checks have passed.

## Data and security boundary

The laptop is local, but it is still another host and process.

The secondary client must receive only the same sanitized model material that the current primary is authorized to receive. It must never receive:

- API tokens;
- Telegram bot token;
- bridge HMAC secret;
- Windows credentials;
- raw environment dumps;
- private TLS keys;
- database files;
- unrestricted filesystem paths;
- cross-tenant material not already authorized for the current model turn.

Preserve tenant, uploader, file-lineage and capability checks before preparing a secondary request.

Do not log prompts or responses in SGLang. Friday telemetry should record only bounded operational facts such as workload, endpoint role, status, latency, token counts and fallback reason.

## V12 boundary

The current V12 code owns exact model/runtime authority for registered primary model profiles. Preserve that binding.

The secondary GPT-OSS endpoint:

```text
does not replace the primary V12 transport
does not share the primary endpoint binding
does not inherit a Qwen capability profile
does not issue a V12 model lease
does not gain tool or effect capabilities
```

It may advise a V12 handler only through a typed, untrusted, read-only input whose absence changes neither authorization nor completion rules.

A future package may add a separate code-owned GPT-OSS profile and live probe. That is explicitly outside this package. Unknown model/runtime pairs remain advisory-only.

## Observability

Add local metrics and diagnostics:

```text
secondary_configured
secondary_state
secondary_last_success_age_sec
secondary_probe_success_total
secondary_probe_failure_total{reason}
secondary_selected_total{workload}
secondary_success_total{workload}
secondary_skipped_total{workload,reason}
secondary_primary_fallback_total{workload,reason}
secondary_latency_seconds{workload}
secondary_queue_wait_seconds{workload}
secondary_protocol_rejection_total{reason}
secondary_context_cap_tokens
secondary_served_model_match
```

Do not use unbounded model names, URLs, exceptions or response text as labels.

Diagnostics should state clearly:

```text
primary: required and healthy/unhealthy
secondary: optional and disabled/healthy/unavailable/cooling
```

An unavailable secondary is not overall health failure. It may be a warning to the owner and a normal state to all other callers.

## Certification battery for the model node

The node is not certified because the container starts.

### Identity and loader

Prove:

- exact HF revision;
- file hash manifest;
- exact container digest;
- exact GPU identity and compute capability;
- native ModelOpt FP4 loader selected;
- FlashInfer CUTLASS FP4 backend selected;
- no CPU offload;
- exact served alias returned by `/v1/models`;
- exact response model alias on completions.

### Protocol

Run at least:

- Russian ordinary answer;
- English ordinary answer;
- Russian structured JSON;
- English structured JSON;
- low, medium and high reasoning instructions;
- one valid tool-call probe;
- one tool-result continuation probe;
- one prompt that should not call a tool;
- long system message;
- multi-turn conversation;
- Unicode, Cyrillic, filenames and numbers;
- stop tokens and maximum-token truncation;
- cancellation and client disconnect.

Raw reasoning must be absent from the public `content` projection and from persisted test evidence.

### Numerical and quality checks

Run deterministic or low-temperature cases covering:

- arithmetic;
- exact extraction;
- date normalization;
- instruction following;
- Russian summarization;
- contradiction detection;
- citation-preservation behavior;
- repeated-token degeneration;
- NaN/Inf detection;
- empty response;
- wrong-language regression;
- schema validity.

Compare against the official source-family behavior or another trusted judge where practical. A quantized model with materially broken quality is rejected even if it is fast.

### Capacity and performance

Measure:

- cold model load;
- first request after boot;
- 4K/8K/12K/16K/24K/32K context ladder;
- TTFT;
- decode tokens per second;
- peak VRAM;
- free VRAM after graph capture;
- GPU power, temperature and throttle state;
- 30 to 60 minute sustained soak;
- at least 100 mixed requests;
- container restart;
- Docker Desktop restart;
- Windows reboot.

### Failure journeys

Prove end to end:

1. Friday starts while laptop is powered off.
2. Friday serves ordinary chat while laptop is powered off.
3. Laptop becomes available and is admitted without restarting Friday.
4. Laptop disappears before secondary admission.
5. Laptop disappears after request submission.
6. SGLang returns 503.
7. SGLang hangs until secondary deadline.
8. SGLang returns malformed JSON.
9. SGLang reports the wrong model alias.
10. Secondary emits invalid tool markup.
11. Secondary is busy.
12. Secondary restarts and eventually recovers through one half-open probe.
13. No scenario replays an effect.
14. No scenario changes primary V12 readiness.
15. Disabling one environment flag restores exact primary-only behavior.

## Friday test plan

Add unit and integration tests before real-node rollout.

Minimum tests:

```text
settings default to secondary disabled
missing secondary settings never fail normal startup
two endpoint clients have independent semaphores and circuit state
secondary connection refusal falls back or skips according to call class
secondary read timeout is bounded below primary timeout
secondary admission saturation does not queue the user
wrong model alias is rejected
reasoning_content is not published or stored
tool calls from advisory secondary are never executed
text-only guard rejects image-bearing messages
required-result path falls back once to primary
optional-advice path does not duplicate primary work
cancellation drains the local HTTP task
cooldown has one half-open recovery probe
V12 remains bound to primary
diagnostics distinguish optional degradation from service failure
metrics contain no prompt content or unbounded labels
```

Use a fake OpenAI-compatible server for deterministic transport tests. Do not make the general test suite depend on the laptop.

The full existing Python, type, lint, security, schema, migration and release gates must remain green. No storage schema migration should be needed for this feature.

## Rollout sequence

### P0: node only

- configure WSL2/Docker GPU path;
- download and pin checkpoint;
- certify SGLang baseline;
- tune context;
- secure LAN endpoint;
- produce evidence;
- no Friday code changes used in production.

### P1: dormant Friday support

- endpoint configuration;
- per-endpoint client abstraction;
- protocol adapter;
- state machine, diagnostics and tests;
- default disabled;
- primary-only behavior byte-for-byte or semantically unchanged where asserted.

### P2: shadow

- copy selected classifier/extractor/critique calls;
- discard secondary output;
- compare latency, schema validity and answer quality;
- no user-visible effect.

### P3: assist for deterministic bounded work

- classification;
- extraction;
- query rewrite;
- bounded summaries;
- primary fallback on every required result.

### P4: optional foreground critique

- selected complex read-only turns only;
- bounded deadline;
- primary final synthesis;
- skip on any secondary failure.

### P5: reconsider scope

Only after evidence, decide whether another read-only workload merits admission. Tool authority, direct publication and effects remain separate future decisions, not the automatic next stage.

## Rollback

Rollback must be one line:

```env
FRIDAY_SECONDARY_LLM_ENABLED=0
```

Stopping or deleting the laptop container must also be safe.

The package must not require:

- database rollback;
- deleting stored data;
- changing the primary model profile;
- moving the primary endpoint;
- restoring a proxy;
- reprocessing the archive.

Keep the previous primary-only construction available until the secondary package has completed shadow and assist soak.

## Required deliverables

The architect should return:

1. Friday code changes and tests.
2. Separate Windows/SGLang deployment bundle.
3. Exact model file manifest and hashes.
4. Exact image digest and runtime version manifest.
5. Capacity tuning report and selected context.
6. Model quality and protocol report.
7. Laptop-off and mid-turn-disconnect evidence.
8. Updated `.env.example`, Compose mapping, diagnostics and operator docs.
9. A compact implementation status document under `outer_sol/`.
10. A rollback demonstration.
11. No credentials in any artifact.

## Definition of done

This package is done only when all of the following are true:

```text
the selected NVFP4 checkpoint runs natively on the laptop GPU
the accepted SGLang image and model revision are immutable
the endpoint is LAN-restricted and authenticated
the measured context cap is recorded
Harmony reasoning never leaks to users or storage
the secondary has no tool/effect authority
Friday boots and works normally with the laptop off
Friday survives the laptop vanishing mid-turn
required auxiliary work falls back to primary
optional advice is skipped cleanly
primary 27B dialogue, tools, V12 and effects remain authoritative
default configuration is primary-only
one flag disables the entire feature
all existing and new gates pass
```

Anything weaker is a demo, not a detachable second brain.

## Source anchors checked while preparing this brief

Revalidate these against the versions actually pinned during implementation:

- Hugging Face model: `shanjiaz/gpt-oss-20b-nvfp4-modelopt`, revision `fb9848e169d5b38cbc00ecf3383283ea1fc33a21`.
- SGLang repository documentation: GPT-OSS deployment, quantization, server arguments and Docker installation.
- SGLang quantization implementation: ModelOpt FP4 loader and SM120 FlashInfer FP4 paths.
- SGLang issues: `#18954`, `#31641`, `#36010` and related SM120/NVFP4 reports.
- Docker Desktop documentation: GPU support on Windows through the WSL2 backend.
- Current Friday source: primary `LLMRouter`, configuration, SGLang profile, V12 model profiles and transport, diagnostics, Compose and architecture documents.
