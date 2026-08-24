# Optional secondary brain implementation status

- Updated: 2026-08-24
- Architecture order:
  `outer_sol/OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`
- Phase: **closed candidate-matrix certification**
- Primary production: Friday `0.207.8` / `da8d11e2ac3c8982e43fa1f104076e922426ac25`,
  schema 38, unchanged
- Current source: `main` at `c726e1a6d6c3826f939c5c586034d6a21fe32917`;
  the accepted secondary-profile registry remains empty and no traffic is sent
  to the laptop
- Rollout policy: default-off; the primary model remains required and final;
  the secondary has no tool, effect, publication or V12 authority

## Durable access and measured host

- The laptop at `192.168.1.35` is reachable through the key-only SSH alias
  `friday-secondary-brain`; OpenSSH is automatic and source-restricted to the
  primary host. No credential or private key is stored in the repository.
- Windows 11 Pro build `26200.9168`; WSL `2.7.3.0`, kernel `6.6.114.1-1`;
  Docker Desktop `4.87.0`, Linux engine `29.7.2`.
- RTX 5080 Laptop GPU: driver `610.88`, 16,303 MiB VRAM and compute capability
  `12.0`; the pinned CUDA container reproduced those facts.
- The laptop has the exact `c726e1a6d6c3826f939c5c586034d6a21fe32917`
  bundle. Its sync receipt is
  `1077a0e39025c7941e4751013baf0134ce494dd3fe0d85f960e70d56a7056dea`;
  the full preflight is green with evidence SHA-256
  `99796c80ff9b6028f41d8ff095706147cd5d5e4ac5275247ae29e5ff0ad2db2b`.
- The observed hardware receipt is
  `7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3`;
  its protected accepted form is
  `0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f`.
  The sealed source is exact and the checkpoint ended with zero running
  containers.

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
- One isolated 4K/BF16 native-MXFP4 canary passed on SM120: SGLang selected
  `Mxfp4MoEMethod`, `flashinfer_mxfp4`, FlashInfer CUTLASS MXFP4 and Triton
  attention; no CPU offload, NaN/Inf, degeneration or Harmony leakage occurred.
  `/models` and completion aliases were exact. The canary evidence SHA-256 is
  `faf4af0c6e429e2ce2b716f84510049bc0fac740c921827e97c4ad14e93f13d0`.
- Canary scope is deliberately `production_accepted=false`. The container and
  transient cache were removed after the run; the GPU is clean.
- Candidate construction is now closed to BF16 or FP8 E4M3 KV (the latter with
  derived implicit-unit scaling), contexts through 65,536 tokens, the exact
  memory/chunk grids, reviewed prefill/decode/sampling backends, page sizes,
  radix/overlap cache policy, hybrid-SWA ratios and decode graphs only at batch
  one. No matrix winner has been measured or accepted.

## Friday checkpoint

- Dormant, fail-soft support covers typed text-only Inbox extraction and bounded
  document map/reduce advice. Every required result has exactly one primary
  fallback; optional advice is skipped; secondary output cannot execute tools
  or publish a final answer.
- Product/runtime admission is profile-v2 and binds the exact source manifest,
  OCI/config identities, package versions, kernels and every candidate engine
  choice. Quality now includes deterministic near-limit recall derived from the
  profile context; endpoint/capacity evidence is bound to exact HTTPS, private
  CA, profile epoch, context and memory without retaining raw prompts.
- Gate at `c726e1a6d6c3826f939c5c586034d6a21fe32917`: 17,689 passed in the
  full parallel run, and the parallel-skipped canvas case passed sequentially,
  for 17,690 verified tests. Only the explicitly configured real Syncthing and
  backup environments remain unrun. The secondary-focused gate passed 206;
  Ruff and mypy are green.

## Paused parent checkpoint

- ICP schema 39 and durable exact selected-archive-evidence replay remain at
  checkpoint `912dc1a`, not deployed. Resume only after the urgent secondary
  package reaches its safe rollout checkpoint.

## Active order

1. Start each exact private-CA candidate and measure the closed matrix without
   promoting a winner early.
2. Pass protocol/quality, context, cold restart, 30-minute/100-request soak and
   failure batteries; accept only the exact evidence-bound profile.
3. Register and release it default-off, then prove shadow and narrow assist,
   including laptop-off and mid-turn disconnect behavior.
4. Resume the paused ICP durable vertical, then the scheduled V12 refinement.

## Not yet claimed

- No secondary profile is accepted or deployed to production Friday yet.
- No matrix winner is selected. Context above 4K, FP8 KV, alternate reviewed
  backends, sustained thermal behavior, restart recovery and end-to-end
  shadow/assist operation remain to be certified live.
