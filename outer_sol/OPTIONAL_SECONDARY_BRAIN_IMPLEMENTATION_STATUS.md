# Optional secondary brain implementation status

- Updated: 2026-08-24
- Architecture order:
  `outer_sol/OPTIONAL_SECONDARY_BRAIN_SGLANG_GPT_OSS_20B_ARCHITECT_BRIEF.md`
- Phase: **P0 node-only discovery and certification in progress**
- Primary production: Friday `0.207.8`, schema 38, unchanged
- Rollout policy: secondary default-off; primary remains required and final;
  secondary has no tool, effect, publication or V12 authority

## Durable access checkpoint

- The laptop at `192.168.1.35` is reachable through the dedicated SSH alias
  `friday-secondary-brain` using a dedicated ED25519 key.
- Windows OpenSSH Server is installed, running and automatic. Password and
  keyboard-interactive SSH authentication are disabled; the host accepts the
  exact public key only.
- The ED25519 host key was compared with the key read locally on Windows before
  it was pinned. TCP 22 is allowed only from the primary host's observed LAN
  address; the broad installer firewall rule is disabled.
- No credential or private key is stored in the repository.
- `192.168.1.35/24` is now a persistent manual address with gateway/DNS
  `192.168.1.1`; a pre-armed DHCP rollback was removed only after fresh SSH,
  gateway, DNS and Docker checks passed.
- Windows Firewall is enabled on all profiles. SSH remains source-scoped to the
  primary host; LAN RDP is retained only as a recovery path.

## Measured P0 facts

- Windows 11 Pro build `26200.9168`; Acer Predator PH18-73; Core Ultra 9 275HX,
  24 logical processors and 68,112,736,256 bytes RAM.
- WSL `2.7.3.0`, kernel `6.6.114.1-1`, Docker Desktop `4.87.0`, Linux engine
  `29.7.2`, Compose `5.4.0`, 24 CPUs and 53,682,892,800 bytes assigned memory.
- RTX 5080 Laptop GPU: driver `610.88`, 16,303 MiB reported VRAM and compute
  capability `12.0`.
- A pinned CUDA 13.0.1 image at
  `nvidia/cuda@sha256:f8ef28f579ea42a44b415d2c5d46f788e6a9b395c6c83f2929416e1fc192c143`
  reproduced the same GPU, driver, VRAM and compute capability inside a Linux
  container. The Python/Torch kernel canary remains pending on the intended
  SGLang image.

## Paused Friday checkpoint

- ICP schema 39 and durable exact selected-archive-evidence replay are preserved
  at local commit `912dc1a` and have not been deployed.
- Restart/exact replay, late denial and source drift are code-owned, model-free
  and source-free on failure. The focused checkpoint gate is 645 passed; Ruff,
  mypy, compileall and diff checks are clean.
- Resume point: complete the full isolated/release gate, then release schema 39
  as a separate small package.

## Active order

1. Record measured Windows, WSL2, Docker, NVIDIA and GPU-container facts.
2. Create the separate pinned deployment bundle and credential-free manifests.
3. Prove the exact GPT-OSS checkpoint and native ModelOpt NVFP4 loader on the
   laptop; tune an honest context cap and run the protocol/quality/soak battery.
4. Implement default-off, independent endpoint/client/scheduler support in
   Friday with sanitized typed advice and bounded fail-soft behavior.
5. Roll out shadow, then narrowly bounded assist only after laptop-off and
   mid-turn-disconnect evidence passes. One flag must restore primary-only mode.

## Not yet claimed

- No SGLang image, loader, model quality, context capacity, GPU-container path,
  thermal soak or failure journey is certified yet.
- No secondary request is currently admitted by production Friday.
