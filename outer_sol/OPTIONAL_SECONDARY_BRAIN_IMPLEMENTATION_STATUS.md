# Optional secondary brain implementation status

- Updated: 2026-08-25
- Architecture order:
  `outer_sol/OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`
- Phase: **bounded optional assist live; physical loss/recovery closed**
- Live production: Friday `0.207.26` /
  `9ab75a82393919e477890b601d243ae7baedad5a`, tree
  `87f05bedd19fe76ccb5928e21b47106caac1660c0bcf4e8994f8c20967d9d2e5`, wheel
  `c59c920e1936cd1cb3a386f062a1aec47a367cc4cce2767f9b148ec214ae43e1`,
  schema 41; immediate predecessor and schema-capable fallback Friday
  `0.207.24` / `9142765647b75d12cea22798df6782a09bc5c4b8`, tree
  `ce654409f09b93cc651543968e81bb7254dd5af48d8698ae7cd06c0084d28f30`,
  wheel `f7710d76e581bdea813c3f56e86b8cf3c53727b5ea180ee2554d03347b6f9cc6`
- Rollout policy: the primary model remains required and final. The secondary
  is live only as bounded optional advice; it has no tool, effect, publication,
  knowledge-write or V12 authority. Live `0.207.26` binds exactly one accepted
  profile and an empty provisional registry. Unavailability skips/falls back to
  the unchanged primary path.

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
- Release `0.207.13` added a mandatory causal request receipt before a physical
  power-loss claim: the full request body must have been submitted to the
  pinned-CA endpoint before transport loss. The live `0.207.26` runner SHA-256
  is `bdfbbc373a021ebf34846c430a0b98e0acba3190f6477dbd25d2e449fffa9fbc`.
- Production atomically activated the exact assist ENV through
  `secondary_shadow_to_assist`; terminal receipt SHA-256 is
  `1b07dac7750d744d0ab8c9998418e58985751966a71b85e65e771ad460d4bf86`.
  Live health is `ok`; the accepted manifest is admitted and demand-probed.
- The fresh assist product receipt hashes to
  `7b4fa2b40f70047020e29e00a74498ee5bef13a2153a0a5e51ce9f9647f79a80`.
  The exact `0.207.26` causal power-cut, off-state and recovered-candidate
  receipts hash to
  `194096eca5718c5071af1fea2414f3539a4ddeefa8ccf7e39ca302fb1f44974b`,
  `b005a6db0cb095419ecdc065b039392e706cf14d52b207fc602dbb2de60eb2b4`
  and `4b037955f83335bc9065e652a6cdb2014607a06ce71e9c9afc5bb98b6de45c49`.
- A fail-closed at-logon gateway publication recovery is installed on the
  laptop and included in source `0.207.26`. It waits for exact LAN/Docker and
  healthy gateway identity, requires two consecutive matching proofs that both
  the `192.168.1.35:8443` publication and listener are absent, and then allows
  at most one restart of only `friday-secondary-gateway`. Inconsistent evidence
  stops recovery; the model runtime is never restarted.

## Friday checkpoint

- The accepted-profile path contains typed private Inbox extraction and bounded
  private document map/reduce seams. Live `0.207.26` runs structured, text-only,
  effect-free bounded assist. Every required result has
  exactly one primary fallback; optional advice is skipped; secondary output
  cannot execute tools or publish a final answer.
- Product/runtime admission is profile-v2 and binds the exact source manifest,
  OCI/config identities, package versions, kernels and every candidate engine
  choice. Quality now includes deterministic near-limit recall derived from the
  profile context; endpoint/capacity evidence is bound to exact HTTPS, private
  CA, profile epoch, context and memory without retaining raw prompts.
- The exact `0.207.24` base passed 18,666 non-UI and 31 UI tests. The bounded
  `0.207.26` hotfix passed its focused release/secondary gate; its wheel also
  reproduced byte-for-byte.

## Document-map expansion implementation checkpoint (not deployed)

- The existing bounded attachment MAP/REDUCE seam is now wired to a separate
  code-owned product policy `gptoss20b-document-map-v1`, manifest SHA-256
  `7d57947d7ecda675e8a4da3f56332baf32484c08c0504afd7fa420b9c6323cd9`.
  It binds the unchanged accepted runtime profile and gateway manifest
  `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`;
  no Windows image, model, launch arguments, served alias or container restart
  is claimed or required by this source package.
- Real document-map calls are text-only, private-lineage checked, read-only,
  concurrency-one and capped by the accepted 4K/512 envelope. Output uses one
  strict code-owned JSON `summary` schema. Invalid schema/content, deadline,
  admission, transport, profile or model identity falls back to the unchanged
  primary map exactly once. Primary still performs final synthesis and owns all
  tools, effects and publication.
- Rollout is independently staged while current Inbox extraction remains in
  assist: `secondary_assist_enable_document_map_shadow` adds discarded
  document-map shadow. V1 deliberately has no assist transition: a later
  release must bind the separate shadow checkpoint to a new policy and operator
  evidence gate before secondary output may influence document mapping. This
  source/operator shadow path is ready, not evidence that it has run in
  production.

## Parallel parent checkpoint

- ICP schema 40, the durable exact archive candidate-selection runtime and the
  bounded body-free DocumentCatalog worker/archive consumer are deployed.
  The next durable conversation/document comparison journey now proceeds while
  the deployed assist remains a separate optional advisory contour.

## Active order

1. Release the document-map policy dormant, activate its discarded shadow
   transition, and build an evidence-bound assist gate from that checkpoint.
2. Retain a separate product-counter outage/recovery drill as operational
   evidence; it is not a prerequisite for ordinary optional operation.
3. Continue the next durable ICP journey and V12 refinement with the secondary
   remaining advisory only.

## Not yet claimed

- The physical receipt proves causal transport loss, unchanged Friday primary
  and exact candidate recovery. It does not claim the optional combined
  `product-state` counter projection for two scheduler fallbacks in that same
  power cycle; that remains a separate drill.
- No secondary tools, effects, publication, knowledge writes or V12 authority
  are claimed.
