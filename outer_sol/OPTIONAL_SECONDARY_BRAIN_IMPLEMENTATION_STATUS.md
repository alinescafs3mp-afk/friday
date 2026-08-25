# Optional secondary brain implementation status

- Updated: 2026-08-25
- Architecture order:
  `outer_sol/OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`
- Phase: **profile accepted; accepted-registry public-shadow release prepared**
- Live production: Friday `0.207.22` /
  `331460d4219ec8a421f1ec0abe668ae989ca9cc5`, tree
  `d16caa76c61f7afe98d9cc8512e62188af65033924f4e5fd166f935094192178`, wheel
  `fee818502c41192dd54475e5622305f6ba730c9b5c1b87de13ea82aab57176c2`,
  schema 41; immediate predecessor and schema-capable fallback Friday
  `0.207.21` / `27b9fc5545e88a38e111170f79d0f548edfbc646`, tree
  `74ca567845d1f5de0656f8be8df2e4302d7d04ccb5de356335dec59514ee5a70`
- Rollout policy: the primary model remains required and final. The secondary
  is enabled only for non-private discarded `shadow/extract`; it has no tool,
  effect, publication or V12 authority. Source `0.207.23` binds exactly one
  accepted profile and an empty provisional registry, while preserving public
  discarded shadow; private shadow/`assist` remain closed.
- Live `0.207.22` now fails soft against the promoted laptop manifest because
  it predates accepted-registry admission; primary health remains `ok`.

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
  and controlled-live evidence, physical failure and profile acceptance are closed.
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
- The measured and accepted finalist is profile
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
  historical and ineligible. The accepted profile manifest SHA-256 is
  `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`;
  accepted physical-failure evidence SHA-256 is
  `9dc72f80caed3320bd154cf1219a8bd6b1339142b690b00dd1cbe1fb05964006`.

## Current production evidence

- The source-bound secondary evidence from
  `1ea5a1dd7e9fab4c483e176726071ed55100721c` passed the 101-case deterministic
  failure battery; receipt SHA-256 is
  `7a66c9a02628f0cc31c0ddfb33f52220386dcfe268d5873292189689abf0fc8b`.
  Earlier source-bound receipts remain history and are ineligible for current
  acceptance.
- Controlled gateway loss, exact recovery, runtime restart and changed runtime
  epoch passed; the receipt SHA-256 is
  `4d344b3d810ebb0e2bb4e7af3c5750f3bdc79a8ed54b55bb1b6a59570440d395`.
- Release `0.207.13` adds a mandatory causal request receipt before a physical
  power-loss claim: the full request body must have been submitted to the
  pinned-CA endpoint before transport loss. The deployed runner SHA-256 is
  `826607fbb48bd3192141a99b3d7ba81d32aa2e31948553d7524760f9eb8b30ac`.
- Production retained the exact public-shadow ENV atomically. Live health is
  `ok`; accepted-registry admission is prepared in source `0.207.23`, not yet
  deployed. No private text is eligible and all shadow output is discarded.

## Friday checkpoint

- The accepted-profile path contains typed private Inbox extraction and bounded
  private document map/reduce seams. Release `0.207.23` deliberately keeps its
  first accepted stage non-private, structured, text-only, effect-free and
  discarded `shadow/extract`. Every required result has
  exactly one primary fallback; optional advice is skipped; secondary output
  cannot execute tools or publish a final answer.
- Product/runtime admission is profile-v2 and binds the exact source manifest,
  OCI/config identities, package versions, kernels and every candidate engine
  choice. Quality now includes deterministic near-limit recall derived from the
  profile context; endpoint/capacity evidence is bound to exact HTTPS, private
  CA, profile epoch, context and memory without retaining raw prompts.
- The exact public-shadow source passed 18,074 non-UI and 31 UI tests. Static
  and focused secondary gates are green.

## Parallel parent checkpoint

- ICP schema 40, the durable exact archive candidate-selection runtime and the
  bounded body-free DocumentCatalog worker/archive consumer are deployed.
  Common effect-envelope/Obsidian reconciliation work proceeds while the
  accepted-registry release is prepared; private shadow remains the next stage.

## Active order

1. Release the exact accepted registry as public discarded shadow and capture
   its immutable production evidence.
2. Prove the separate private product-shadow stage before narrow assist,
   then repeat the physical cycle with product-linked counters in assist,
   including laptop-off and mid-turn disconnect behavior.
3. Continue the common effect envelope and scheduled V12 refinement without
   widening secondary authority.

## Not yet claimed

- The exact 4K/BF16 profile is accepted, but its registry release is not yet
  deployed. Live production remains public shadow and fails soft while the
  older release cannot admit the promoted manifest.
- Private-shadow and assist operation, including product-linked physical-cycle
  evidence, remain pending.
