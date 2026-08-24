# Optional secondary brain implementation status

- Updated: 2026-08-24
- Architecture order:
  `outer_sol/OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`
- Phase: **provisional finalist live certification**
- Primary production: Friday `0.207.9` /
  `2b197e1e467e93a085a1b4cc330fbda8b5b7b982`, schema 39; schema-capable
  fallback `f1426ca561f8914574cebf3a69f8dde83f79b568`
- Current implementation checkpoint: `main` at
  `1e3834dd5d987f84c6ca6a490c0cd9b3ac2756ed`
- Rollout policy: default-off; the primary model remains required and final;
  the secondary has no tool, effect, publication or V12 authority. The
  accepted registry is empty, `assist` admission is closed and production sends
  no traffic to the laptop

## Durable access and measured host

- The laptop at `192.168.1.35` is reachable through the key-only SSH alias
  `friday-secondary-brain`; OpenSSH is automatic and source-restricted to the
  primary host. No credential or private key is stored in the repository.
- Windows 11 Pro build `26200.9168`; WSL `2.7.3.0`, kernel `6.6.114.1-1`;
  Docker Desktop `4.87.0`, Linux engine `29.7.2`.
- RTX 5080 Laptop GPU: driver `610.88`, 16,303 MiB VRAM and compute capability
  `12.0`; the pinned CUDA container reproduced those facts.
- The current repository checkpoint and laptop finalist use the exact profile
  and runtime identities below. The fresh epoch-D capacity-v2 warm trial passed;
  a new 30-minute soak is active and this status does not claim its result before
  the final receipt closes.
- The observed hardware receipt is
  `7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3`;
  its protected accepted form is
  `0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f`.
  The earlier sealed-source preflight ended with zero running containers; the
  exact finalist containers are now running for the active certification chain.

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
  failed protocol/capacity gate. Status remains `measured_not_yet_certified`
  pending the active fresh soak and a matching cold-restart v2 receipt. Earlier
  passed soak and streaming capacity-v1 receipts are retained as historical
  measurements only and are ineligible for v2 acceptance. The finalist remains
  `candidate`, is registered only for non-private discarded `shadow/extract`,
  and is not an accepted production profile.

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
- The latest completed full non-UI gate before the soak-protocol-only follow-up
  commits passed 17,951 tests; one explicitly configured real-Syncthing case
  remained environment-gated. Static and focused secondary gates are green.

## Paused parent checkpoint

- ICP schema 39 and the narrow durable exact selected-archive-evidence replay
  are deployed in `0.207.9`. Broader ICP work remains paused until the urgent
  secondary package reaches its safe rollout checkpoint.

## Active order

1. Finish active `soak.epoch-d.full.7c1f742.json`, then run the identical
   capacity-v2 protocol after a cold runtime restart; do not accept either from
   an in-progress or historical receipt.
2. Release the exact current finalist code default-off, then pass deterministic,
   controlled-live and physical laptop-loss batteries from that sealed source;
   accept only the evidence-bound finalist.
3. Register the resulting accepted manifest in a separate default-off release,
   then prove the separate private product-shadow stage before narrow assist,
   including laptop-off and mid-turn disconnect behavior.
4. Resume the paused ICP durable vertical, then the scheduled V12 refinement.

## Not yet claimed

- No secondary profile is accepted and no production Friday request is sent to
  the laptop. The deployed feature remains default-off and assist is blocked.
- The exact 4K/BF16 finalist is provisional. The active fresh epoch-D soak,
  matching cold-restart capacity-v2 receipt, failure evidence, physical
  power-loss witness and end-to-end shadow/assist operation remain to be
  completed and accepted.
