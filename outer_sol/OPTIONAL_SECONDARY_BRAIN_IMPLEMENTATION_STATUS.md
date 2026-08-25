# Optional secondary brain implementation status

- Updated: 2026-08-25
- Architecture order:
  `outer_sol/OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`
- Phase: **provisional public shadow live; physical/profile acceptance pending**
- Live production: Friday `0.207.12` /
  `a6e3c093939116d0e953db72c60de5dd54438812`, tree
  `ce35dcf1bb68347f99e7e8037291043cee3d1b2651795766b0f8ba5f80e751c8`, wheel
  `fcf502d5058c27b1b9c671f94ec5604281cac9123dc3705e17089cd54e72f6ad`,
  schema 39; default-off transition fallback `7913c633954d3da2df042b598cd08b4fb4e94a91`
  / tree `ae7dfe4d659b8dbc43246d7f38d823ae86d8c562e36c3be1713965060f66ff40`
- Rollout policy: the primary model remains required and final. The secondary
  is enabled only for non-private discarded `shadow/extract`; it has no tool,
  effect, publication or V12 authority. The accepted registry is empty and
  private shadow/`assist` admission remain closed.

## Durable access and measured host

- The laptop at `192.168.1.35` is reachable through the key-only SSH alias
  `friday-secondary-brain`; OpenSSH is automatic and source-restricted to the
  primary host. No credential or private key is stored in the repository.
- Windows 11 Pro build `26200.9168`; WSL `2.7.3.0`, kernel `6.6.114.1-1`;
  Docker Desktop `4.87.0`, Linux engine `29.7.2`.
- RTX 5080 Laptop GPU: driver `610.88`, 16,303 MiB VRAM and compute capability
  `12.0`; the pinned CUDA container reproduced those facts.
- The current released source and laptop finalist use the exact profile and
  runtime identities below. Fresh warm, 30-minute soak, cold-restart capacity-v2
  and controlled-live evidence are closed; physical/profile acceptance is not.
- The observed hardware receipt is
  `7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3`;
  its protected accepted form is
  `0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f`.
  The earlier sealed-source preflight ended with zero running containers; later
  certification receipts bind the exact finalist runtime without asserting its
  current container state.

## Model/runtime checkpoint

- The community NVFP4 checkpoint was rejected for deterministic degeneration.
- The internal ModelOpt conversion produced the expected byte/tensor inventory,
  but the pinned 0.5.16 loader dropped all 144 MoE scales and used incompatible
  layout/bias metadata. Its NaN/Inf runtime and the whole conversion/compatibility
  deployment contour are rejected and removed from active source.
- The selected fallback is the sealed official source
  `openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee`:
  14 exact files, 13,789,264,674 bytes, manifest SHA-256
  `438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9`.
- The exact runtime is SGLang `0.5.17` image
  `lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405`,
  source revision `29481685462732237d80d86076d6563e1f658102`.
- The measured provisional finalist is profile
  `gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`,
  candidate-manifest SHA-256
  `51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439`
  and engine binding
  `2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`.
- Its exact engine projection is native MXFP4/BF16 with BF16 KV, 4,096 total
  context tokens, 512 output tokens, one running request,
  `mem_fraction_static=0.96`, chunked prefill 256, page size 1, Triton
  attention, PyTorch sampling, `flashinfer_mxfp4` MoE, enabled radix/overlap
  cache, hybrid SWA ratio 0.80, full decode CUDA graph at batch one and disabled
  prefill graph. Weight and KV CPU offload remain disabled.
- The exact finalist has passed protocol and quality. The fresh capacity-v2
  epoch-D warm receipt `capacity.v2.epoch-d.warm.7c1f742.json`, SHA-256
  `b317e964eced1c0a80d5d8f4cc7fcb388d60598c16dfbeb9f320f1076fa97719`,
  passed seven non-streaming exact 512-token repeats at runtime epoch
  `1787601267.06`: median end-to-end completion rate `108.497563` tokens/s and
  minimum free GPU memory 1,294 MiB. The harness is fail-fast on the first
  failed protocol/capacity gate.
- Epoch-D soak passed in 1,800.218 seconds with 4,467 requests, zero failures,
  minimum free GPU memory 1,294 MiB and peak temperature 75 C. Its SHA-256 is
  `852673984f6705c148d0a92957d3c2f2fd5360925b0fddd2225eb8b631a8983a`.
- After a real cold restart, epoch `1787603294.09` passed capacity v2 7/7 at
  exact 512 completion tokens: median end-to-end rate `106.733375` tokens/s,
  minimum free GPU memory 1,296 MiB and receipt SHA-256
  `9c60611b939098020faa4f9077debde3bec96c9ded2bffc3c3385fc94d5ffa87`.
  The post-cold probe, 29/29 quality and unchanged quality-epoch receipts hash to
  `b3a88138d43ac799aa113e1b53f8cc9b0c0c106d0decaf71726a438e29ddeec5`,
  `7bb0e3aa9b48dd95afdf8a1c226fa5b7eae6212f45f72966d82344cd3227e824`
  and `b7e345962770e26f45b90f33c9ac7180be4c72dc4427247025a23fefe197a310`.
- The verified v2 wrapper accepted the exact warm/cold/soak capacity chain;
  accepted-capacity SHA-256 is
  `519b5912428f491dc65928c5ba2d2e33a6408566fe5f3496501ce2e760b9205e`.
  The earlier operational wrapper's v1 schema assertion was superseded and
  produced no false success. Earlier streaming capacity-v1 receipts remain
  historical and ineligible. The finalist itself remains `candidate`, is
  registered only for non-private discarded `shadow/extract`, and is not an
  accepted production profile.

## Current production evidence

- Exact source `7913c633954d3da2df042b598cd08b4fb4e94a91` passed a fresh endpoint
  probe; receipt SHA-256 is
  `73a8e0036b81aecfaa2ac2a6f4a323ec60956d9947aa7a198c3e059adfd41269`.
- Its 101-case deterministic failure receipt hashes to
  `669da889b7fadb681df17aca7f9de68507bfc43bf029147bd47ff5d7929f8ff2`.
  The old `aaae455` receipt is retained only as history and is ineligible for
  acceptance against the current source epoch.
- Controlled gateway loss, exact recovery, runtime restart and changed runtime
  epoch passed; the receipt SHA-256 is
  `26cf1c88f683aa1b0b85184a68cb7e321ea59460845ffdfe5bbaeb498dfff0bc`.
- Distinct-candidate activation `a6e3c09` published the exact public-shadow ENV
  atomically. Production health is `ok`; diagnostics bind
  `profile_admission=provisional_shadow`, matching profile/manifest/model and a
  healthy endpoint. No private text is eligible and all shadow output is
  discarded.

## Friday checkpoint

- The dormant accepted-profile path contains typed private Inbox extraction and
  bounded private document map/reduce seams. Neither is eligible for the
  provisional finalist: its only admission is non-private, structured,
  text-only, effect-free discarded `shadow/extract`. Every required result has
  exactly one primary fallback; optional advice is skipped; secondary output
  cannot execute tools or publish a final answer.
- Product/runtime admission is profile-v2 and binds the exact source manifest,
  OCI/config identities, package versions, kernels and every candidate engine
  choice. Quality now includes deterministic near-limit recall derived from the
  profile context; endpoint/capacity evidence is bound to exact HTTPS, private
  CA, profile epoch, context and memory without retaining raw prompts.
- The exact public-shadow source passed 18,067 non-UI and 31 UI tests. Static
  and focused secondary gates are green.

## Paused parent checkpoint

- ICP schema 39 and the narrow durable exact selected-archive-evidence replay
  remain deployed. Broader ICP work remains paused until the urgent
  secondary package reaches its safe rollout checkpoint.

## Active order

1. Strengthen the physical witness so a mid-turn loss is causally bound to a
   code-owned submitted request, then perform the owner-observed laptop cut/on
   without restarting Friday.
2. Run `accept-failure` and `accept-profile`, then register the resulting exact
   accepted manifest in a separate immutable release.
3. Prove the separate private product-shadow stage before narrow assist,
   then repeat the physical cycle with product-linked counters in assist,
   including laptop-off and mid-turn disconnect behavior.
4. Resume the paused ICP durable vertical, then the scheduled V12 refinement.

## Not yet claimed

- No secondary profile is accepted. Production performs bounded exact-profile
  admission probes and may send only non-private discarded `shadow/extract`;
  private text and assist are blocked.
- The exact 4K/BF16 finalist is provisional. Capacity v2 is accepted, but
  physical evidence, profile acceptance, registry promotion and end-to-end
  private-shadow/assist operation remain pending.
