# GPT-OSS abliterated replacement

The replacement is pinned to
`huihui-ai/Huihui-gpt-oss-20b-mxfp4-abliterated-v2@79f64a520a4a0275f639c1a47d9a5614a8a54477`.
It keeps the current native-MXFP4 SGLang/Harmony runtime surface. The laptop
stage is rollback-safe and does not modify or delete the old model volume or
bundle.

Exact staged identities:

- bundle: `C:\ProgramData\FridaySecondary\bundle-ablit-79f64a52`;
- volume: `friday-secondary-source-gptoss20b-ablit-79f64a52`;
- source-manifest SHA-256:
  `8dfc3a50d1a9407fbb07dde5f1b494157664c75cdd0e140ecb85f7d55732a296`;
- candidate profile:
  `gptoss20b-d4c2207151c7507f9d71a1d3d5d387d6ae98bb89b04f3171ba667098c2ad2d25`;
- candidate-profile SHA-256:
  `612ed412143458fc32bcee2b78cfa66afdaec0f947b7c6b78422afa6d9fd5a64`.

The profile is deliberately `candidate`; all four evidence hashes are zero.
Do not relabel it as accepted and do not reuse evidence from the official-model
profile. This revision admits the exact identity only through Friday's
`PROVISIONAL_SHADOW` registry, which restricts it to discarded public
`shadow/extract` traffic. The release environment must select it explicitly:

```text
FRIDAY_SECONDARY_LLM_MODEL=friday-secondary-gptoss20b-d4c2207151c7507f9d71a1d3d5d387d6ae98bb89b04f3171ba667098c2ad2d25
FRIDAY_SECONDARY_LLM_PROFILE=gptoss20b-d4c2207151c7507f9d71a1d3d5d387d6ae98bb89b04f3171ba667098c2ad2d25
FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0
FRIDAY_SECONDARY_LLM_MODE=shadow
FRIDAY_SECONDARY_LLM_WORKLOADS=extract
```

After the matching Friday release is installed, the single laptop activation
command is:

```powershell
docker compose -p friday-secondary-brain --env-file C:\ProgramData\FridaySecondary\bundle-ablit-79f64a52\.env.stage -f C:\ProgramData\FridaySecondary\bundle-ablit-79f64a52\compose.yml up -d --force-recreate
```

Rollback remains one command and preserves both bundles and volumes:

```powershell
docker compose -p friday-secondary-brain --env-file C:\ProgramData\FridaySecondary\bundle\.env -f C:\ProgramData\FridaySecondary\bundle\compose.yml up -d --force-recreate
```

For a pre-release transient proof, `scripts\transient-candidate-smoke.ps1`
performs the candidate activation, exact profile-epoch check, bounded Russian
chat and forced function-call shape check, then always recreates and probes the
old deployment. Its report retains only identities, timings, token counts and
content hashes.

The 2026-08-26 proof used the exact full decode graph (`full`, max batch `1`,
batch list `[1]`): boot, profile epoch, `/models` and bounded chat passed. A
forced `tool_choice` request returned HTTP 400, after which the operator restored
and probed the old deployment successfully. Friday's current GPT-OSS protocol
adapter does not send tools, so this does not block provisional `shadow/extract`;
the candidate is not certified as a direct tool-calling endpoint. Any future
tool-enabled route must first resolve the candidate chat-template/Harmony
compatibility and earn separate evidence.
