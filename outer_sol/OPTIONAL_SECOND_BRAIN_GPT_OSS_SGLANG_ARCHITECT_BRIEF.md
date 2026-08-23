# Optional Second Brain: GPT-OSS 20B NVFP4 on SGLang

> Document ID: FRIDAY-AUX-LLM-001  
> Status: External architecture and implementation handoff, draft v0.1  
> Date: 24 August 2026  
> Observed Friday repository checkpoint: `main` at `ef00a457b0b203febd2eab494b63f605353c35b7`  
> Observed Friday production checkpoint: Friday `0.207.8` / `da8d11e`, schema 38  
> Candidate SGLang checkpoint at the time of writing: stable `v0.5.17` / `2948168`; the deployed image must be pinned by digest after live validation  
> Audience: Friday system architect, implementation lead, and deployment operator  
> Scope: one optional Windows-laptop inference node plus the smallest safe Friday runtime changes needed to use it  
> Primary invariant: Friday must remain fully functional on the existing 27B model when the laptop, Docker Desktop, SGLang, the network, or the auxiliary model is absent or unhealthy

## How to use this brief

This is an implementation brief, not permission to replace the current primary model or to widen model authority.

Before changing code:

1. Re-read the current canonical project and Interaction Control Plane status in `outer_sol/`.
2. Reconcile this document against the current `main`; the commit above is only an observation point.
3. Inspect the exact current construction and call paths around:
   - `friday/agent_runtime/llm.py`;
   - `friday/config/__init__.py`;
   - `friday/server.py`;
   - `friday/model_profiles.py`;
   - `friday/v12_model_transport.py`;
   - `.env.example`;
   - `docker-compose.yml`;
   - diagnostics, sentinel, workers, ingestion, document-map, and answer verification call sites.
4. Preserve the primary Qwen3.8 27B behavior and its exact V12 attestation path.
5. Deliver the laptop endpoint independently before enabling any Friday routing to it.
6. Introduce the auxiliary path disabled by default, prove every failure mode with mocks, then move through shadow and advisory rollout stages.
7. Do not put credentials, private certificate keys, model API keys, Windows account data, prompts, or personal archive excerpts in the repository.

The operator supplied interactive Windows account credentials out of band. They are deliberately not repeated here. They are not an application secret and must not be used as the SGLang API credential. Before remote administration is enabled, rotate the weak bootstrap password and prefer an SSH key.

## Operator objective

The desired user-visible behavior is simple:

```text
auxiliary GPT-OSS endpoint healthy
    -> Friday uses it for eligible text-only work and independent advice

auxiliary endpoint unavailable, asleep, starting, restarting, overloaded, wrong, or broken
    -> Friday immediately degrades to the existing primary-only behavior
    -> no failed startup
    -> no failed health check
    -> no broken conversation
    -> no lost tool or effect semantics
    -> no requirement to restart Friday when the endpoint later returns
```

The auxiliary machine is:

```text
Windows laptop
static LAN address: 192.168.1.35
CPU: Intel Core Ultra 9 275HX
RAM: 64 GB
GPU: NVIDIA GeForce RTX 5080 Laptop GPU, expected 16 GB VRAM
runtime: Docker Desktop with the WSL 2 Linux-container backend
```

Every hardware fact, including VRAM size and CUDA compute capability, remains subject to live inspection. The expected consumer Blackwell target is SM120, but the deployment manifest must record what the container actually observes.

The requested model/runtime target is:

```text
model family: OpenAI GPT-OSS 20B
deployment weight format: genuine NVIDIA NVFP4, not a renamed MXFP4 checkpoint
runtime: SGLang in a pinned Linux container
priority: maximize useful stable context after weights and required runtime headroom
availability: optional
authority: advisory and read-only
```

## Executive decision

Build two independent failure domains:

```text
PRIMARY FRIDAY HOST                                  OPTIONAL LAPTOP NODE
──────────────────────────────────────               ─────────────────────────────
Friday backend                                       Docker Desktop / WSL 2
existing Qwen3.8 27B endpoint                        pinned SGLang image
mandatory primary endpoint                           GPT-OSS 20B NVFP4 checkpoint
OptionalModelPool                                    TLS/auth gateway
code-owned routing and fallback                      metrics + deployment witness
all permissions and effects                          no Friday database or tools
```

Within Friday:

```text
                         ┌──────────────────────┐
user turn / worker job ->│ code-owned workload │
                         │ classification       │
                         └──────────┬───────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │ OptionalModelPool           │
                     │ primary is mandatory        │
                     │ auxiliary is opportunistic  │
                     └──────────────┬──────────────┘
                                    │
                 ┌──────────────────┴──────────────────┐
                 │                                     │
       ┌─────────▼──────────┐                ┌─────────▼──────────┐
       │ Qwen3.8 27B        │                │ GPT-OSS 20B        │
       │ primary            │                │ auxiliary          │
       │ multimodal         │                │ text-only          │
       │ final publication  │                │ utility / critic   │
       │ tools and effects  │                │ no effects         │
       └────────────────────┘                └────────────────────┘
```

Do not place an opaque round-robin or failover proxy in front of both models. The application must know which exact model handled which exact task. Model identity, prompt dialect, capabilities, deadlines, context limits, failure behavior, and authority are different.

Do not deploy a second Friday backend, worker set, Telegram bridge, SQLite database, or complete Friday Compose stack on the laptop. The laptop runs inference infrastructure only.

## Critical model-format correction

The official `openai/gpt-oss-20b` checkpoint is not NVFP4. OpenAI ships and evaluates the model with MXFP4 quantization of the MoE weights. The official card describes approximately 21B total parameters, 3.6B active parameters per token, Harmony formatting, and operation within 16 GB in the official MXFP4 form.

The operator explicitly selected native NVFP4 on Blackwell. Therefore the implementation must not silently do any of the following:

```text
load the official MXFP4 checkpoint and label it NVFP4
use an MXFP4 MoE backend and call the result NVFP4
load BF16/fake-quantized tensors and report only quantization metadata
perform unsupported online conversion on every boot
accept an arbitrary third-party repository because "NVFP4" appears in its name
fall back to BF16, MXFP4, GGUF, or CPU offload while reporting the target as met
```

The production checkpoint is admissible only through one of these paths, in order of preference:

1. An official OpenAI or NVIDIA Model Optimizer NVFP4 GPT-OSS 20B checkpoint, pinned to an immutable revision and accompanied by a verifiable quantization configuration.
2. A reproducible NVIDIA Model Optimizer conversion from a verified source checkpoint, exported once to a SGLang-compatible Hugging Face layout, with conversion code, tool versions, calibration manifest, input and output hashes, and project-specific quality evidence.
3. A third-party NVFP4 checkpoint only after the architect independently proves provenance, real compressed tensor format, loader compatibility, expected GPU memory, quality, and exact immutable file hashes. Popularity or a model-card claim is not proof.

If only an MXFP4 source is available, explicitly determine whether the proposed conversion is a supported dequantize/requantize path or a lossy second quantization. Record that fact. Do not describe double quantization as equivalent to a conversion from unquantized source weights.

If genuine NVFP4 cannot be made correct on the laptop and the pinned SGLang build, return a technical blocker with evidence. Do not make the acceptance test green by changing the meaning of "native NVFP4."

## Why this model is an auxiliary brain, not a replacement

The two models are complementary:

| Property | Primary Qwen3.8 27B | Auxiliary GPT-OSS 20B |
|---|---|---|
| Product role | Existing authoritative runtime | Optional specialist |
| Model shape | Larger 27B multimodal model | MoE, about 21B total and 3.6B active |
| Modality | Text and image in the current Friday profile | Text only |
| Prompt dialect | Qwen chat/reasoning/tool conventions | OpenAI Harmony and GPT-OSS reasoning/tool conventions |
| Best initial use | Dialogue, synthesis, planning, tool ownership, vision, effects | Classification, extraction, query rewrite, bounded summaries, critique, independent verification |
| Failure consequence | Primary outage | Optional degradation only |
| Effect authority | Existing code-owned paths | None |
| Final publication | Yes | No in the first release |

Active MoE parameters do not make GPT-OSS a simple "3.6B model," nor do total parameters make it interchangeable with a dense 20B model. Treat measured behavior, not parameter arithmetic, as authority.

The strongest architectural value is model diversity. GPT-OSS can catch some omissions or contradictions made by Qwen because it is a different family with a different prompt format and error profile. That independence is useful only if Friday does not give it the final word or silently fall back to the same primary model while claiming an independent verification.

## Authority and trust model

Friday remains the sole owner of:

```text
tenant and actor identity
capability checks
retrieval authority
file and message provenance
tool availability
effect classification
idempotency and receipts
database writes
final publication
model routing
fallback decisions
deadlines and cancellation
endpoint admission
deployment attestation
```

The primary model may continue to request tools through the existing checked path.

The auxiliary model may:

```text
return a bounded text candidate
return schema-validated extraction/classification output
return a bounded critique or verifier verdict
suggest a tool name or plan only as untrusted advisory data
```

The auxiliary model must never:

```text
execute a Friday tool
own a tool loop
cause a database, file, web, MCP, Telegram, reminder, or code-execution effect
publish directly to the user
see a raw image
see raw secret material
grant itself a stronger route
select its own endpoint
claim V12 capabilities through configuration
turn its reasoning trace into a stored or user-visible answer
```

SGLang's built-in GPT-OSS web search, Python execution, demo tool server, and external MCP client support must remain disabled. Those facilities would create a second, unaudited execution kernel outside Friday's permissions and effect receipts.

## Workload contract

Introduce a code-owned workload vocabulary. Names may be refined, but the semantic split must remain explicit.

```python
class ModelWorkload(StrEnum):
    DIALOGUE = "dialogue"
    FINAL_SYNTHESIS = "final_synthesis"
    TURN_PLANNING = "turn_planning"
    TOOL_SELECTION = "tool_selection"
    VISION = "vision"

    CLASSIFY = "classify"
    EXTRACT = "extract"
    QUERY_REWRITE = "query_rewrite"
    SUMMARIZE = "summarize"
    DOCUMENT_MAP_LEAF = "document_map_leaf"
    CRITIQUE = "critique"
    VERIFY = "verify"
```

Route by workload and authority, not by prompt text and not merely by `foreground` versus `background`.

Initial routing matrix:

| Workload | Preferred endpoint | Auxiliary failure behavior | Notes |
|---|---|---|---|
| Dialogue | Primary | N/A | Do not call auxiliary for trivial greetings |
| Final synthesis | Primary | N/A | One public answer owner |
| Turn planning | Primary | N/A | Especially for tools or effects |
| Tool selection | Primary | N/A | Existing checked tool path only |
| Vision/raw image | Primary | N/A | GPT-OSS is text-only |
| Classification | Auxiliary | Retry once on primary | Strict schema |
| Extraction | Auxiliary | Retry once on primary | Bounded text only |
| Query rewrite | Auxiliary | Retry once on primary | No direct search authority |
| Summary | Auxiliary when bounded | Retry once on primary | Source IDs remain code-owned |
| Document-map leaf | Auxiliary | Retry once on primary | Primary performs final reduce/synthesis |
| Critique | Auxiliary | Skip as unavailable | Preserve model independence |
| Verification | Auxiliary | Return `unknown`/`skipped` | Never call primary and label it independent |

For complex foreground knowledge or research answers, the architect may add a bounded auxiliary verifier after primary drafting. The verifier returns a closed verdict such as:

```json
{
  "status": "pass | repair | unknown",
  "issue_codes": ["unsupported_claim", "missed_constraint"],
  "repair_instructions": ["bounded instruction without source text"]
}
```

The primary remains responsible for any repair and final answer. A verifier timeout must not erase an otherwise valid primary answer.

"Use it when available" means that eligible utility work preferentially uses the auxiliary endpoint and selected complex answers receive independent advice. It does not mean sending every greeting twice or adding a mandatory second generation to every turn.

## Request and result contracts

Add a model request envelope owned by application code:

```python
@dataclass(frozen=True, slots=True)
class ModelRequest:
    workload: ModelWorkload
    priority: Literal["foreground", "background"]
    effect_class: Literal["none", "read", "write", "high"]
    modality: Literal["text", "vision"]
    messages: tuple[Mapping[str, object], ...]
    max_output_tokens: int
    absolute_deadline_monotonic: float
    require_structured_output: bool
    require_independent_model: bool = False
```

The scheduler must reject auxiliary routing unless all of these are true:

```text
auxiliary mode allows the workload
effect_class is none or read-only advisory
modality is text
request fits the attested context tier
endpoint circuit permits an attempt
endpoint identity is admitted
request contains no prohibited private carrier
```

Normalize endpoint output into a common result that preserves provenance without exposing secrets or chain-of-thought:

```python
@dataclass(frozen=True, slots=True)
class ModelResult:
    visible_content: str
    structured_output: Mapping[str, object] | None
    tool_calls_present: bool
    reasoning_present: bool
    endpoint_role: Literal["primary", "auxiliary"]
    served_model_alias: str
    fallback_reason: str | None
    usage: Mapping[str, int]
```

Do not store raw auxiliary reasoning. Do not copy it into logs, audit rows, conversation messages, verifier notes, or error strings.

## Endpoint/client architecture

The current `LLMRouter` is bound to one `FridaySettings`, one base URL, one model alias, one API key, Qwen-safe prompt normalization, one set of semaphores, and one circuit state. Extract the endpoint concerns without a flag-day rewrite.

Recommended shape:

```text
EndpointConfig
    immutable URL, alias, API key reference, CA file, limits, capabilities

ModelProtocolAdapter
    request projection
    prompt dialect
    response normalization
    reasoning separation
    tool-call normalization
    context estimation rules

OpenAIEndpointClient
    one endpoint
    one circuit breaker
    one semaphore set
    retries
    deadlines
    cancellation
    exact response-model check

OptionalModelPool
    deterministic workload routing
    primary fallback
    shadow/advisory policy
    telemetry
```

Adapters:

```text
QwenProtocolAdapter
    preserves current behavior and tests

GptOssHarmonyAdapter
    preserves Harmony semantics
    uses GPT-OSS reasoning/tool parsing
    never publishes reasoning
    treats emitted tool calls as advisory only
```

The first implementation may retain the `LLMRouter` class name for the primary path and introduce the generic client beneath it. Avoid a broad rename whose only value is aesthetic. Existing primary call sites and V12 transport must remain behaviorally identical.

The pool may be composed at application startup, but auxiliary endpoint validation is non-blocking. Syntactically invalid configured values may fail configuration validation; an unreachable optional endpoint may not fail startup.

## GPT-OSS protocol requirements

GPT-OSS is trained for the Harmony response format. Do not reuse Qwen-specific system-message or thinking cleanup rules without a dedicated compatibility test.

Initial transport decision:

1. Prefer the OpenAI Chat Completions endpoint if the pinned SGLang version correctly applies Harmony, returns a stable visible answer, separates `reasoning_content`, supports required structured output, and reports the exact served alias.
2. If Chat Completions loses required Harmony channels or produces unstable tool/structured output, implement a dedicated auxiliary Responses API adapter.
3. Do not change the primary endpoint protocol merely to accommodate GPT-OSS.
4. Keep reasoning effort code-owned by workload:
   - `low` for classification and query rewriting;
   - `medium` for extraction and summary;
   - `medium` or a measured `high` for selected critique/verification.
5. Cap both reasoning and visible output through endpoint-specific budgets.
6. Test Russian prompts, mixed Russian/English metadata, JSON, long context, and cancellation. English-only smoke tests are insufficient.

A native tool parser may be enabled in SGLang so protocol output is correctly separated. Friday still must not execute auxiliary tool calls. `tool_calls_present=True` is normally a schema failure for extraction/verifier work unless the specific advisory contract explicitly permits a tool suggestion.

## V12 boundary

Do not weaken `V12ModelGate`, reuse the Qwen profile, or let the auxiliary endpoint answer under the `dispatcher` alias.

Initial release:

```text
auxiliary GPT-OSS is outside V12 route authority
no V12 lease
no effect class
no vision capability
no native tool authority
no final publication authority
```

If a later package needs V12-aware auxiliary planning, add a separate code-owned profile and fresh live probe suite with:

```text
exact runtime profile
exact served alias
exact endpoint binding
exact process epoch
text-only capability
read-only/advisory effects
its own context tier
its own cancellation proof
its own planner contract
```

Configuration and `/v1/models` output alone must never mint capabilities.

Use the existing V12 deployment-witness pattern as a design reference, not as permission to make the auxiliary masquerade as the primary.

## Configuration

Preserve all existing primary variables for backward compatibility. Add an independent optional namespace. Suggested names:

```env
# Off by default. Exact values: disabled | shadow | utility | advisory
FRIDAY_AUX_LLM_MODE=disabled

FRIDAY_AUX_LLM_BASE_URL=https://192.168.1.35:8443/v1
FRIDAY_AUX_LLM_MODEL=friday-gpt-oss-20b-nvfp4
FRIDAY_AUX_LLM_API_KEY=
FRIDAY_AUX_LLM_CA_FILE=

FRIDAY_AUX_LLM_CONNECT_TIMEOUT_SEC=1.5
FRIDAY_AUX_LLM_REQUEST_TIMEOUT_SEC=30
FRIDAY_AUX_LLM_COOLDOWN_SEC=60
FRIDAY_AUX_LLM_MAX_TOKENS=1024
FRIDAY_AUX_LLM_VERIFIED_CONTEXT_TOKENS=0
FRIDAY_AUX_LLM_MAX_CONCURRENCY=1
FRIDAY_AUX_LLM_SHADOW_SAMPLE_RATE=0.0
```

Semantics:

- Missing or `disabled` means no auxiliary client is constructed and current behavior is exact.
- `shadow` sends a sampled subset of eligible requests, discards outputs from product decisions, and records bounded metrics only.
- `utility` routes classifier/extractor/rewrite/summary/map work to the auxiliary endpoint with primary fallback.
- `advisory` adds independent critique/verification for selected routes.
- A configured but unreachable endpoint is an optional degraded component, not a configuration error.
- `VERIFIED_CONTEXT_TOKENS=0` must prevent live routing until a measured tier is installed. Do not infer it from the model's nominal 128K configuration.
- Auxiliary CA trust is separate from Friday backend TLS trust. Do not reuse private keys.
- Secrets remain in the live environment or a protected secret file, never `.env.example`, Compose YAML, a command line recorded in history, or the deployment witness.

The main Friday `docker-compose.yml` should pass these values to the backend but must not attempt to create, start, stop, or depend on the laptop service.

## Circuit breaker and fallback semantics

Each endpoint owns independent state. An auxiliary failure must never open the primary circuit or consume the primary silent cooldown.

Minimum auxiliary states:

```text
disabled
closed
open
half_open
misconfigured
identity_rejected
```

Behavior:

| Failure | Utility workload | Critique/verifier workload |
|---|---|---|
| Connection refused / no route | Immediate primary fallback | `unknown`, no verification |
| Connect timeout | Primary fallback | `unknown` |
| Read timeout | Cancel/drain, then primary fallback | `unknown` |
| HTTP 429 / 503 / 504 | Respect bounded retry policy, then primary | `unknown` |
| HTTP 401 / 403 | Open as configuration fault, no retry storm, primary | `unknown` |
| Wrong model alias | Reject endpoint identity, primary | `unknown` |
| Malformed JSON/protocol | Primary fallback | `unknown` |
| Repeated-token degeneration | Primary fallback | `unknown` |
| GPU OOM / engine restart | Primary fallback and cooldown | `unknown` |
| Endpoint returns later | Half-open real probe, then normal use | Normal use without Friday restart |

Use a short connect timeout so a sleeping laptop does not add a long pause to every turn. Keep a bounded per-role total deadline. Do not spend the primary model's full timeout proving that an optional machine is asleep.

A fallback is safe because the auxiliary path has no side effects. Cancel and locally drain the failed request before starting the primary fallback where possible. The remote SGLang process may still finish discarded generation; it must have no authority to act on the result.

Do not retry the same silent endpoint three times before fallback. One fast retry may be justified for a connection reset or transient 503, but a full read timeout is enough to open the optional circuit.

Recovery must be demand-driven or performed by a lightweight bounded probe. Friday must not require a restart when the laptop returns.

## Diagnostics and observability

Expose sanitized auxiliary state in diagnostics:

```json
{
  "auxiliary_model": {
    "configured": true,
    "mode": "utility",
    "status": "open",
    "reason_code": "connect_failed",
    "served_model_match": false,
    "verified_context_tokens": 24576,
    "last_success_age_sec": 420,
    "circuit_retry_after_sec": 18
  }
}
```

Never expose:

```text
base URL credentials
API keys
certificate private keys
Windows account names/passwords
prompts or outputs
raw model server errors
model filesystem paths
private checkpoint download tokens
```

Suggested metrics:

```text
friday_model_requests_total{role,workload,outcome}
friday_model_request_latency_seconds{role,workload}
friday_model_fallback_total{workload,reason}
friday_model_circuit_state{role}
friday_aux_verification_total{status}
friday_aux_shadow_total{comparison}
```

Use bounded label vocabularies. Do not put arbitrary exception text, URLs, model paths, tenant IDs, or user text in labels.

Overall `/api/health` remains healthy when only the auxiliary endpoint is down. Diagnostics may show an optional warning. Sentinel notifications, if added, must be rate-limited and should alert only after a configured sustained outage, not whenever the operator intentionally closes the laptop.

## Laptop deployment boundary

Create a separate deployment package, for example:

```text
deploy/auxiliary-gpt-oss/
    README.md
    compose.yml
    .env.example
    config/
        sglang.template.yaml
        gateway.template.conf
    scripts/
        bootstrap.ps1
        doctor.ps1
        download_model.ps1
        calibrate_context.py
        smoke.py
        soak.py
        render_manifest.py
    manifests/
        README.md
```

Do not place live secrets or generated machine manifests in Git.

The deployment is one Compose project on the laptop, not part of Friday's primary Compose project.

Recommended network shape:

```text
LAN
  |
  | HTTPS + bearer, firewall source restricted
  v
TLS/auth gateway on 192.168.1.35:8443
  |
  | private Compose network
  v
SGLang on container port 30000
```

SGLang should not publish its plain HTTP port directly to the LAN when the gateway is present.

The gateway may be Caddy, nginx, Envoy, or a minimal reviewed proxy. It must:

```text
terminate TLS with an IP-SAN certificate or a stable local hostname
require a dedicated random bearer credential
limit request and response body sizes
set bounded upstream timeouts
disable redirects to arbitrary origins
expose only required inference/health/metrics/witness paths
not log prompt or response bodies
```

The SGLang API key should also be set. Defense in depth is preferred; the gateway and SGLang key may be distinct.

The Windows firewall must allow the inference port only from the primary Friday host address. Discover and record that source address during deployment. Do not expose the endpoint to the WAN or to the whole local subnet by default.

The static address belongs to Windows, not to the container. Use normal Docker Desktop port publication/NAT. Do not introduce macvlan or a second LAN identity.

## Windows, WSL 2, and Docker Desktop discovery

Interactive credentials alone are not proof that remote administration is possible. The architect must verify:

```text
Windows edition/build
administrator rights
OpenSSH or RDP availability
Docker Desktop version and startup behavior
WSL version and kernel
WSL 2 backend enabled
NVIDIA Windows driver version
container-visible GPU and compute capability
available SSD capacity
power/sleep behavior on AC
firewall source address for the primary Friday host
```

Suggested discovery commands, adjusted to the live machine:

```powershell
winver
wsl --version
wsl --status
wsl --list --verbose
wsl --update
nvidia-smi
docker version
docker info
Get-ComputerInfo
Get-Volume
powercfg /a
Get-NetIPAddress -AddressFamily IPv4
Get-NetFirewallProfile
```

Validate container GPU access with a pinned CUDA image compatible with the intended SGLang image:

```powershell
docker run --rm --gpus all <pinned-nvidia-cuda-image> nvidia-smi
```

Then validate in Python inside the intended image:

```python
import torch
print(torch.__version__)
print(torch.version.cuda)
print(torch.cuda.get_device_name(0))
print(torch.cuda.get_device_properties(0).total_memory)
print(torch.cuda.get_device_capability(0))
```

Docker Desktop GPU support on Windows requires the WSL 2 backend. Use the Windows NVIDIA driver. Do not install a Linux NVIDIA display driver inside WSL; the Windows driver is projected into WSL.

Prefer Docker named volumes or storage inside the WSL ext4 virtual disk for model weights and hot caches. Avoid putting the model hot path on a Windows NTFS bind mount unless measured loading performance is acceptable.

For unattended operation:

- enable Docker Desktop start at sign-in if desired;
- use `restart: unless-stopped`;
- test a real Windows reboot and user sign-in;
- optionally disable automatic sleep on AC power;
- do not make Friday responsible for waking or starting the laptop.

The laptop is intentionally disposable from Friday's point of view. Sleep, shutdown, reboot, driver update, and Docker restart are normal auxiliary outages.

## Remote administration and credential handling

Preferred bootstrap:

1. Enable Windows OpenSSH Server locally or through an existing trusted channel.
2. Restrict port 22 by Windows firewall to the administrator's source address.
3. Install an SSH public key for the operator/architect.
4. Confirm key authentication.
5. Disable password authentication or retain it only for a short, documented recovery window.
6. Rotate the bootstrap password before network exposure.

RDP may remain a manual recovery path, but automation should use SSH/PowerShell remoting with keys rather than embedding a password.

Never place the supplied Windows credentials in:

```text
this repository
a task prompt committed to Git
PowerShell command history
Compose environment
Docker labels
SGLang arguments
Friday configuration
logs or evidence bundles
```

A separate, generated SGLang bearer token of at least 32 random bytes is required.

## Checkpoint production and admission

The architect must produce a machine-readable model manifest. Suggested fields:

```json
{
  "schema": "friday.aux-model-manifest.v1",
  "base_model": "openai/gpt-oss-20b",
  "base_revision": "<immutable commit>",
  "source_quantization": "mxfp4 | bf16 | other",
  "conversion_supported": true,
  "conversion_tool": "nvidia-modelopt",
  "conversion_tool_version": "<exact>",
  "conversion_container_digest": "sha256:<...>",
  "calibration_dataset_manifest_sha256": "<...>",
  "export_format": "huggingface-modelopt-nvfp4",
  "quantization": "nvfp4",
  "weight_files_sha256": {"file": "sha256"},
  "tokenizer_files_sha256": {"file": "sha256"},
  "served_model_alias": "friday-gpt-oss-20b-nvfp4",
  "quality_report_sha256": "<...>"
}
```

The production startup must load an already exported checkpoint. Online `--quantize-and-serve` may be used only as an exploratory experiment, not as the normal boot path.

Admission evidence must prove:

```text
immutable source and output revisions
actual quantization config recognized by the pinned SGLang loader
no silent unquantized layers outside the declared mixed recipe
on-disk compressed representation, not only fake-quantized BF16 tensors
expected GPU-resident memory
successful generation through GPT-OSS/Harmony
quality compared with the official MXFP4 reference
correct exact served alias
```

Where some non-expert tensors remain BF16 or FP8 by recipe, describe the deployment honestly as a mixed ModelOpt NVFP4 recipe and list the tensor classes. "Native NVFP4" must refer to real FP4 execution of the intended weight classes, not to every tensor being four bits.

## SGLang version and image policy

At the observation date, SGLang `v0.5.17` is the latest stable release and includes current SM120-oriented FP4 backend selection, GPT-OSS support, ModelOpt FP4 options, NVFP4 KV-cache options, CUDA graph controls, and metrics. It is the first candidate, not an automatic final choice.

Rules:

```text
never use :latest in production
pin the image by sha256 digest
record SGLang version and git commit
record base CUDA, PyTorch, FlashInfer, cuDNN, and ModelOpt versions
record the Windows NVIDIA driver
resolve every command-line flag against `python -m sglang.launch_server --help`
```

If stable `v0.5.17` cannot correctly load the chosen NVFP4 checkpoint on SM120, a nightly or custom image is acceptable only when:

```text
the exact source commit is pinned
the built image digest is pinned
the build recipe is committed
the same correctness and soak gates pass
the reason stable is insufficient is documented
rollback to the stable experiment remains available
```

Do not select a newer nightly merely because it boots or benchmarks faster.

## SGLang feature policy

"Maximum SGLang features" means maximum useful, compatible, measured features inside the memory and trust budget. It does not mean enabling every switch.

### Baseline features to enable or preserve

Subject to exact pinned-version syntax and live correctness:

- OpenAI-compatible API.
- Exact `--served-model-name friday-gpt-oss-20b-nvfp4`.
- `--reasoning-parser gpt-oss`.
- `--tool-call-parser gpt-oss` for protocol separation only.
- RadixAttention/prefix caching, which is enabled unless explicitly disabled.
- Overlap scheduler, which is enabled unless explicitly disabled.
- Chunked prefill, initially test 2048 and 4096 tokens.
- Single-request scheduling to maximize context:
  - `--max-running-requests 1`;
  - `--prefill-max-requests 1`;
  - a small bounded queue.
- CUDA graph for decode at batch size 1 if it does not reduce the selected context tier.
- Model checksum verification.
- API and separate admin API keys.
- Prometheus metrics.
- Cache usage reporting.
- Watchdog with a measured bound.
- Request-body logging disabled.
- `--strip-thinking-cache` if verified, so finished reasoning output is not retained as a reusable cache suffix.
- A grammar backend for strict JSON output where compatible.
- Hardware-appropriate FP4 GEMM and MoE backends selected by correctness first, then latency.
- A bounded healthcheck and restart policy.

Current SGLang documentation says the FP4 GEMM `auto` path selects FlashInfer CUTLASS on SM120. Treat that as a candidate to verify, not as proof of the actual kernel selected. Compare at least:

```text
fp4-gemm-backend: auto
fp4-gemm-backend: flashinfer_cutlass
fp4-gemm-backend: flashinfer_cudnn, only with a compatible CUDA/cuDNN stack
```

For MoE, test `auto` and the actual NVFP4-capable SM120 candidates exposed by the pinned image. Do not use the `flashinfer_mxfp4` backend for an NVFP4 acceptance claim.

### KV-cache candidates

Weight quantization and KV-cache quantization are separate decisions.

Evaluate in this order:

```text
BF16 KV
    quality baseline, smallest context

FP8 E4M3 KV
    likely strong context/quality compromise

NVFP4 KV
    maximum context candidate on Blackwell
    only if long-context and quality gates pass
```

The current SGLang argument surface includes `--kv-cache-dtype nvfp4` and requires a sufficiently new CUDA/PyTorch stack. A successful boot is not evidence of acceptable long-context recall. Do not select NVFP4 KV solely because the weights are NVFP4.

### Features to evaluate, not assume

Each must have an A/B result and must not reduce the chosen verified context unless the operator explicitly accepts the trade:

- prefill piecewise/breakable CUDA graph;
- strict-thinking token filtering;
- tokenizer batching;
- n-gram speculative decoding without a draft model;
- `torch.compile`, which current SGLang documentation still marks experimental/out of maintenance;
- HiCache/host KV cache;
- host memory offload;
- alternative attention backends;
- NVFP4 KV cache;
- Rust frontend;
- experimental custom kernel/nightly combinations.

### Features disabled in the initial production profile

- SGLang demo tool server.
- Exa web search.
- SGLang Python execution.
- External MCP tool servers.
- Any server-side autonomous tool loop.
- EAGLE3 draft model by default.
- Expert parallelism, tensor parallelism, data parallelism, and distributed communication features on one GPU.
- Model Gateway/router for a single backend.
- CPU weight offload.
- HiCache as a substitute for a stable GPU-only baseline.
- Request/response body logging.
- LoRA hot loading.
- Online quantization on every boot.
- Unpinned remote code.
- Any option whose only evidence is that startup completed.

EAGLE3 is specifically deferred because a draft model consumes memory that the operator prioritized for context. It may be reconsidered only if the draft fits without reducing the verified context tier and yields a measured latency win.

## Illustrative SGLang configuration

The architect must render the final configuration from the exact pinned image's argument schema. This is a design sketch, not copy-paste authority:

```yaml
model-path: /models/gpt-oss-20b-nvfp4
served-model-name: friday-gpt-oss-20b-nvfp4
weight-version: <immutable-model-manifest-id>
model-checksum: /manifests/checksums.json

host: 0.0.0.0
port: 30000
api-key: <injected-secret>
admin-api-key: <different-injected-secret>

tokenizer-mode: auto
model-impl: auto
load-format: safetensors
quantization: <loader-required-for-exported-modelopt-nvfp4>

context-length: <measured>
mem-fraction-static: <measured>
max-total-tokens: <measured>
max-running-requests: 1
max-queued-requests: 8
prefill-max-requests: 1
chunked-prefill-size: <2048-or-4096-after-test>

kv-cache-dtype: <nvfp4-or-fp8_e4m3-after-quality-test>
reasoning-parser: gpt-oss
tool-call-parser: gpt-oss
grammar-backend: xgrammar

fp4-gemm-backend: auto
moe-runner-backend: auto

enable-metrics: true
enable-cache-report: true
strip-thinking-cache: true
sleep-on-idle: true
watchdog-timeout: <measured>

cuda-graph-config:
  decode:
    backend: full
    max_bs: 1
    bs: [1]
  prefill:
    backend: <disabled-or-tc_piecewise-after-test>
```

The API key cannot live in this checked-in YAML. Render a protected local config or inject the secret through a non-logged mechanism.

## Illustrative Compose shape

Also a design sketch:

```yaml
name: friday-aux-gpt-oss

services:
  sglang:
    image: ${SGLANG_IMAGE_DIGEST:?pin exact digest}
    restart: unless-stopped
    ipc: host
    shm_size: "8gb"
    environment:
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
    command:
      - python3
      - -m
      - sglang.launch_server
      - --config
      - /config/sglang.yaml
    volumes:
      - model-store:/models:ro
      - model-cache:/root/.cache
      - ./rendered/sglang.yaml:/config/sglang.yaml:ro
      - ./rendered/checksums.json:/manifests/checksums.json:ro
    expose:
      - "30000"
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:30000/health', timeout=3)"]
      interval: 15s
      timeout: 5s
      retries: 20
      start_period: 300s
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  gateway:
    image: ${GATEWAY_IMAGE_DIGEST:?pin exact digest}
    restart: unless-stopped
    depends_on:
      sglang:
        condition: service_healthy
    ports:
      - "192.168.1.35:8443:8443"
    volumes:
      - ./rendered/gateway.conf:/etc/gateway/config:ro
      - gateway-pki:/pki
    networks:
      - default

volumes:
  model-store:
  model-cache:
  gateway-pki:
```

Confirm the actual Docker Desktop support for `ipc`, healthcheck tools, device reservation syntax, and host-IP binding. Resolve incompatibilities in the deployment package, not by exposing SGLang unauthenticated.

## Context and memory calibration

Do not promise the model's nominal 128K context. The verified tier is determined on the actual laptop under WSL 2/WDDM, with the actual NVFP4 checkpoint and selected runtime features.

"Everything left goes to context" means:

```text
weights remain GPU resident
no draft model by default
no CPU weight offload
concurrency is one
only useful CUDA graphs are captured
KV cache receives the remaining safe allocation
operational headroom remains for the display driver, kernels, workspaces, and transient allocations
```

Using 100 percent of reported VRAM is not a valid goal. A configuration that boots once and OOMs when Windows, the display, a CUDA graph, or a long prefill allocates memory is not stable.

Calibration procedure:

1. Record idle Windows and container-visible VRAM.
2. Load the exact model at an 8K context with conservative static memory fraction.
3. Prove basic generation, Harmony, Russian text, JSON, reasoning separation, cancellation, and exact alias.
4. Establish BF16 KV as a quality baseline at the largest stable tier practical.
5. Test FP8 E4M3 KV.
6. Test NVFP4 KV.
7. For each KV type:
   - increase context in declared tiers, for example 8K, 12K, 16K, 24K, 32K, then binary search;
   - test chunked prefill 2048 and 4096;
   - test decode CUDA graph batch 1;
   - reserve empirical VRAM headroom, initially at least 1 to 1.5 GB or 8 to 12 percent, then justify the final value from observation;
   - run 90 to 98 percent context prompts with at least 512 to 1024 generated tokens;
   - issue cancellation during long prefill and long decode;
   - run normal Windows display activity;
   - run repeated cold starts;
   - run a multi-hour soak.
8. Select the largest tier with zero OOMs, hangs, parser failures, silent truncation, and unacceptable quality regression.
9. Store the selected context, KV type, static fraction, token-pool size, graph settings, driver, image digest, and benchmark results in the deployment manifest.
10. Configure Friday with this verified tier only.

The endpoint and Friday must reject an over-context request before starting generation. Friday's request fitting must use the auxiliary endpoint's own context size, not the primary Qwen profile.

## Quality and compatibility battery

Create a fixed, versioned evaluation bundle before choosing the checkpoint/backend/KV combination.

Minimum categories:

```text
Russian instruction following
mixed Russian/English entities and metadata
strict JSON classification
strict JSON extraction
query rewrite with dates and names
summary faithfulness
document-map leaf synthesis
contradiction detection
unsupported-claim detection
Harmony channel separation
tool-call parser separation
long-context retrieval at multiple positions
repeated-token degeneration
cancellation
wrong alias and malformed response
```

Compare:

```text
official GPT-OSS 20B MXFP4 reference
candidate NVFP4 weights with BF16 KV
candidate NVFP4 weights with FP8 KV
candidate NVFP4 weights with NVFP4 KV
```

Predeclare acceptance thresholds before reading final scores. At minimum:

- no reasoning trace in visible content;
- no tool execution;
- no silent JSON acceptance after schema failure;
- no material regression in project utility tasks versus the official reference;
- long-context tests pass at the declared verified tier;
- deterministic zero-temperature cases remain stable enough for classifiers;
- all checkpoint and runtime identities are reproducible.

Record both quality and latency. A fast model that produces invalid structured output is not a useful utility endpoint.

## Friday implementation stages

### Stage 0: Baseline and discovery

- Freeze a primary-only behavior fixture.
- Enumerate every model call site and classify it by workload, modality, effect, and fallback safety.
- Identify Qwen-specific code currently embedded in `LLMRouter`.
- Reconcile current answer verification semantics.
- Produce the laptop hardware/runtime report.
- Resolve the NVFP4 checkpoint provenance path.

Exit: no product code changed; evidence identifies the exact seams.

### Stage 1: Standalone laptop endpoint

- Build the separate Compose package.
- Produce the model and runtime manifests.
- Run the full SGLang smoke, context calibration, quality battery, security checks, restart tests, and soak.
- Prove TLS, API authentication, firewall restriction, exact alias, metrics, and witness.

Exit: the endpoint is independently usable, but Friday does not know it exists.

### Stage 2: Disabled auxiliary abstraction

- Add endpoint config, adapter, optional pool, diagnostics, and metrics.
- Preserve primary env names and current primary behavior.
- Default mode remains `disabled`.
- Use mock HTTP transports for every error class.
- Avoid a database migration unless a demonstrated durable requirement exists.

Exit: primary-only full suite is unchanged.

### Stage 3: Shadow

- Configure the real endpoint in `shadow`.
- Sample eligible text-only requests.
- Discard auxiliary outputs from product decisions.
- Record bounded status, parse success, latency, and predeclared comparison metrics.
- Do not persist raw prompts/outputs as a shadow corpus unless the operator explicitly enables a protected local evaluation path.

Exit: stable real traffic evidence without user-visible effect.

### Stage 4: Utility routing

Enable, one workload at a time:

```text
query rewrite
classification
extraction
bounded summary
document-map leaves
```

Each route gets:

```text
exact schema
exact deadline
exact context cap
primary fallback
failure metric
quality gate
rollback switch
```

Exit: laptop-off tests prove primary-only continuation.

### Stage 5: Independent critique and verification

- Add selected complex-answer verification.
- A failed verifier returns `unknown`; it does not invoke primary as an "independent" replacement.
- Primary owns repair and final publication.
- Measure added latency and false-repair rate.
- Keep an immediate per-route kill switch.

Exit: independent advice improves measured outcomes without making the endpoint mandatory.

### Stage 6: Optional future work

Only after evidence:

```text
a separate V12 auxiliary profile
more workloads
n-gram speculation
EAGLE3 if it costs no accepted context
HiCache
Responses API migration for the auxiliary adapter
```

None are required for the first useful release.

## Failure-injection matrix

Automate at least these cases:

```text
auxiliary mode disabled
laptop powered off
Windows asleep
Docker Desktop stopped
container absent
container starting slowly
TCP refused
TCP black-holed
TLS certificate expired
TLS hostname/IP mismatch
unknown CA
wrong API key
wrong admin key
wrong served alias
MXFP4 endpoint presented under NVFP4 alias
deployment witness mismatch
HTTP 400
HTTP 401
HTTP 429 with Retry-After
HTTP 500/502/503/504
read timeout before first byte
disconnect during stream
malformed JSON
empty answer
reasoning-only answer
tool call where schema forbids it
repeated-token loop
over-context rejection
GPU OOM and container restart
endpoint recovery while Friday stays running
simultaneous primary and auxiliary failures
```

For every optional failure, assert:

```text
Friday process remains alive
health remains primary-healthy
no user-visible raw exception
no secret in logs
no effect occurs
primary fallback happens where defined
verifier becomes unknown where defined
auxiliary circuit state changes independently
later recovery requires no Friday restart
```

## Security requirements

- LAN-only endpoint.
- TLS for prompt/response traffic.
- Source-IP firewall restriction.
- Dedicated random model API key.
- Separate admin API key not given to normal Friday inference calls.
- No built-in web, Python, MCP, or demo tool server.
- No request/response body logs.
- No raw chain-of-thought persistence.
- No unreviewed `trust_remote_code`.
- Pinned image digests and model revisions.
- Checksum verification.
- Model and container provenance manifest.
- Secret scanning of the deployment package and Friday diff.
- Least-privilege file mounts.
- No access from the laptop container to Friday's database, archive, SMB shares, NAS, Docker socket, or host filesystem outside declared model/config/cache paths.
- Gateway and SGLang administrative endpoints restricted.
- Certificate private keys remain on the laptop.
- Windows bootstrap password rotated and removed from automation.

The auxiliary endpoint processes personal material selected by Friday. Local network placement does not make plaintext HTTP acceptable as the final design.

## Rollback

Rollback must not require a database restore or code revert:

```env
FRIDAY_AUX_LLM_MODE=disabled
```

After backend restart, behavior must match the primary-only baseline exactly.

Stopping the laptop Compose project must also be a safe operational rollback. Friday may log one bounded optional degradation event, then use the primary without repeated warning storms.

Prefer no schema migration in the first package. If durable state is later justified, keep it non-authoritative and backward-compatible.

## Deliverables

The architect should deliver:

### Architecture and code

- endpoint/client protocol and adapters;
- optional model pool and workload contracts;
- configuration and validation;
- circuit breaker, recovery, and fallback;
- sanitized diagnostics and metrics;
- tests and failure injection;
- updated architecture/operations/security documentation;
- release and rollback notes.

### Laptop deployment

- separate Compose project;
- pinned image build or digest;
- SGLang rendered config;
- TLS/auth gateway;
- Windows bootstrap and doctor scripts;
- model download/conversion tooling;
- context calibration tool;
- smoke and soak suites;
- firewall/power/startup runbook;
- no live secrets in Git.

### Evidence

- hardware and driver manifest;
- SGLang/container dependency manifest;
- model provenance and checksum manifest;
- NVFP4 loader/execution proof;
- context/KV calibration report;
- project quality comparison;
- latency report;
- restart/sleep/network failure report;
- security scan;
- full Friday regression gate;
- operator handoff with exact commands and rollback.

## Acceptance criteria

The package is accepted only when all are true:

1. Friday starts and serves normal requests with no auxiliary configuration.
2. Friday starts and serves normal requests when the auxiliary endpoint is configured but unreachable.
3. Turning off the laptop during an eligible request yields bounded fallback or verifier `unknown`, not a failed turn.
4. Turning the laptop back on restores auxiliary use without restarting Friday.
5. The primary Qwen3.8 27B remains the only owner of final dialogue, vision, tools, and effects in the first release.
6. Auxiliary tool calls cannot execute.
7. Auxiliary reasoning is never shown or stored.
8. Raw images are never sent to GPT-OSS.
9. Wrong model alias, wrong manifest, wrong quantization claim, or wrong process epoch prevents auxiliary admission.
10. The production model is proven genuine NVFP4 under the documented mixed recipe; MXFP4 or fake quantization cannot pass under the NVFP4 name.
11. The SGLang image and model are pinned immutably.
12. The largest advertised context tier has passed long-prompt, generation, cancellation, restart, display-load, and soak tests.
13. The selected KV-cache dtype has passed the project quality battery.
14. Docker Desktop/WSL reboot and sleep behavior is documented and does not affect primary Friday availability.
15. TLS, bearer auth, source firewall, and secret handling pass.
16. Built-in SGLang web/Python/MCP tools are absent.
17. Primary and auxiliary circuit breakers are independent.
18. Optional endpoint diagnostics cannot make overall health fail.
19. Existing Friday tests and release gates pass.
20. `FRIDAY_AUX_LLM_MODE=disabled` restores the exact primary-only behavior without data migration.
21. No supplied Windows credential or generated secret exists in the repository history.
22. The operator receives a concise runbook for start, stop, update, doctor, context recalibration, credential rotation, and rollback.

## Explicit non-goals

Do not build:

```text
a generic multi-agent swarm
a second Friday instance
a distributed database
a cross-machine tool executor
a transparent model load balancer
an autonomous SGLang tool environment
a requirement that the laptop remain awake
a new V12 authority package in the first release
a model marketplace
automatic acceptance of arbitrary Hugging Face checkpoints
a context claim based only on config.json
```

## Questions the implementation handoff must answer

The architect must answer these with evidence, not leave them as operator guesswork:

1. What exact GPU compute capability and usable VRAM does the SGLang container observe?
2. What exact immutable NVFP4 checkpoint is used, and how was it produced?
3. Is conversion from the available source officially supported, or is it double quantization?
4. What tensor classes remain outside NVFP4 in the deployed recipe?
5. Which SGLang image digest, commit, CUDA, PyTorch, FlashInfer, cuDNN, and ModelOpt versions are pinned?
6. Which FP4 GEMM, MoE, and attention kernels are actually selected on SM120?
7. What is the largest stable context for BF16, FP8, and NVFP4 KV?
8. Which KV type was chosen and what quality delta did it cause?
9. How much VRAM headroom remains under ordinary Windows display activity?
10. Which utility routes materially improve latency or throughput?
11. Does independent verification improve answer quality enough to justify its added latency?
12. What exact failure bound does a sleeping laptop add before fallback?
13. How is the endpoint admitted and its quantization claim attested?
14. How are TLS trust, API key rotation, and Windows remote administration operated?
15. What one-line action returns Friday to exact primary-only behavior?

## Primary references

Use current official documentation at implementation time and pin what was actually tested:

- OpenAI GPT-OSS 20B model card: <https://huggingface.co/openai/gpt-oss-20b>
- SGLang GPT-OSS usage: <https://docs.sglang.io/docs/basic_usage/gpt_oss>
- SGLang server arguments: <https://docs.sglang.io/docs/advanced_features/server_arguments>
- SGLang `v0.5.17` release observation: <https://github.com/sgl-project/sglang/releases/tag/v0.5.17>
- NVIDIA Model Optimizer: <https://github.com/NVIDIA/Model-Optimizer>
- Docker Desktop Windows GPU support: <https://docs.docker.com/desktop/features/gpu/>
- NVIDIA CUDA on WSL guide: <https://docs.nvidia.com/cuda/archive/13.0.2/wsl-user-guide/index.html>

Current documentation is not a substitute for the exact pinned image's `--help`, live server report, model manifest, and tests.
