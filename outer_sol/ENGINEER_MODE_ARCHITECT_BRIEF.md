# Engineer Mode: Architect Implementation Brief

> **Historical, non-normative implementation brief.** This file preserves the
> proposal that preceded the production candidate. Names, flows and scope below
> may be stale and do not grant runtime authority. The shipped defensive v1 and
> its acceptance requirements are defined by
> [`docs/ENGINEER_MODE.md`](../docs/ENGINEER_MODE.md); that contract and the code
> take precedence.

> Document ID: FRIDAY-ENGINEER-001
> Status: External architecture handoff, draft v0.1
> Date: 25 August 2026
> Audience: Friday system architect and implementation lead
> Scope: an owner-only workbench for static analysis of binaries the owner
> sends through Telegram, on-the-fly byte-level mutation of those artifacts,
> and allowlisted host reconnaissance of the operator's own network.
> Product boundary: this is a first-party organ plus one extra conversation
> mode. It is not a new agent, not a new judge, and not a production release
> package. It must not interrupt the active ICP/V12/secondary-brain course.

## How to use this brief

Build the smallest thing that matches the operator experience below. Do not
open a second runtime, do not add a WorkGraph, and do not teach the V12
planner about this mode.

The operator experience is:

```text
/engineer
    -> Friday stays Friday, without answer-verifier / V12 citation judges
    -> drop an exe, apk, elf, dll, or similar
    -> receive a structured analysis report
    -> ask for a byte patch or a ZIP/APK entry rewrite
    -> receive a new file; the original Raw Object is untouched
    -> name a host on the firm network
    -> receive a recon report (open ports, banners, TLS, obvious weakness)
```

The operator will run live probes on their own machines and watch the
traffic themselves. This brief does not ask Friday to become an exploit
framework.

The live optional secondary brain (GPT-OSS-20B, bounded `assist`, typed
`extract` only) is already on the primary host. Engineer mode must use it
the same way Inbox advice does: as an optional advisory EXTRACT over a
code-owned JSON finding list. It still has no tools, no effects, no
publication, and no V12 authority. Laptop-off or any admission failure
keeps the exact primary-only report.

## Isolation from concurrent work

Sol is implementing ICP/V12 golden-journey and secondary-brain release
evidence on the same tree.

Do not edit:

- `handoffs/Sol/` or `handoffs/SolGoodman/`
- `outer_sol/PROJECT_IMPLEMENTATION_STATUS.md`
- `outer_sol/OPTIONAL_SECONDARY_BRAIN*`
- `friday/interaction_control_plane/`
- V12 route handlers (`orchestration/file_read.py`, `archive_read.py`,
  planner internals, selected-archive explanation)
- production identity files Sol currently has dirty (`CHANGELOG.md`,
  `friday/__init__.py`, `pyproject.toml`, `README.md`,
  `docs/OPERATIONS.md`, `docs/RELEASE_CHECKLIST.md`)

Do not bump the Friday version for this work. It is not a release package.

## Executive decision

```text
engineer conversation mode
    +
engineer organ (capabilities + three tools)
    +
legacy AgentRuntime (no V12, no answer verifier)
```

Do not build:

```text
a second personality or jailbroken prompt stack
a new LLM loop
exploit payloads, PoCs, or an attack runtime
a CVE correlator or vulnerability scanner product
a decompiler, emulator, or debugger
APK/PE re-signing
a fourth judge that "just looks at binaries"
schema changes
```

## Operator identity

Same Friday. Same voice. Same Telegram surface.

What is removed in this mode:

- V12 file-read / archive-read / news verifiers
- the ordinary answer-verifier ("judge") pass
- ICP pending-durable and archive-candidate playbooks
  (they already require `interaction_mode == "dialogue"`)

What is not removed:

- capability checks
- tenant and exact-uploader file reads
- audit rows for mutating tools
- SSRF-style host allowlisting
- Inbox / knowledge-graph review for ordinary material
  (engineer files still land as Raw uploads; they are not auto-promoted
  to canonical knowledge by this mode)

## Mode and command

Add `engineer` to `CONVERSATION_MODES`.

Aliases accepted by `normalize_conversation_mode`:

- `engineer`
- `engeneer` (the operator's spelling)
- `eng` is not an alias; keep the name explicit

Telegram:

- advertised command: `/engineer`
- handled alias: `/engeneer`
- `/help` and `/status` gain one line
- switching is the existing `POST /api/conversations/channel/mode`

Owner-only. `engineer.use` has empty `default_presets`, so only the owner
preset receives it. The Telegram owner private chat is already bound to
`LEGACY_OWNER_USER_ID` via `FRIDAY_TELEGRAM_OWNER_CHAT_IDS`. Anyone else
gets 403 and a short refusal.

`POST /api/conversations/channel/mode` with `mode=engineer` must
`_require(..., "engineer.use")` after the ordinary `chat.use` check.

## Routing

At `OrchestrationRouter.chat`, if the requested mode is `engineer` or
`engeneer`, go straight to the legacy runtime. Do not plan, do not
shadow-plan, do not run a V12 file-read handler on the dropped binary.

`AgentRuntime` already refuses archive-candidate / message-window /
archive-evidence playbooks unless `interaction_mode == "dialogue"`.
Adding the fourth mode therefore keeps those Sol-owned paths closed
without editing them.

`_verify_response` returns `skipped` when `context.interaction_mode` is
`engineer`. No repair loop, no citation judge.

`MODE_GUIDANCE["engineer"]` tells the model: this is a workbench, call
the engineer tools, do not invent PE/ELF structures, do not claim an
exploit was executed.

Tool budget: same order as research, `(12, 5)`.

## Organ

`friday/organs/engineer/` registered in `build_registry` and
`BUILTIN_ORGAN_NAMES`.

Capabilities (`source="organ"`):

| security_id                 | risk | purpose                         |
|-----------------------------|------|---------------------------------|
| `engineer.use`              | 2    | enter the mode                  |
| `engineer.artifact.analyze` | 2    | static analysis of owned files  |
| `engineer.artifact.patch`   | 3    | byte/ZIP mutation, new file     |
| `engineer.host.audit`       | 3    | allowlisted host recon          |

Three tools, dialogue scope only:

1. `engineer_analyze_artifact` (`observe`) — `raw_id` of an owned file.
2. `engineer_patch_artifact` (`mutate`) — `raw_id` plus a closed list of
   patch operations; returns a generated `_attachment`. Original Raw
   bytes are never rewritten.
3. `engineer_hunt` / `engineer_audit_host` / `engineer_http_enum` /
   `engineer_dns` — the owner-named host is enough. Concurrent TCP,
   banners, TLS, HTTP surfaces, optional nmap/dig/file.
4. Auto-hunt on the engineer turn: extract hosts/URLs from speech and
   analyse dropped binaries before the model speaks.

No organ router. No worker. No HITL: the owner is the operator, and
the extra judge is exactly what this mode removes. Audit logging of
mutating calls remains.

## Artifact analysis (code-owned)

Pure functions over bytes. The model never sees a filesystem path.

Detect and report, fail-soft on truncated or hostile inputs:

- hashes (MD5, SHA-1, SHA-256), size, Shannon entropy
- printable ASCII / UTF-16LE strings (bounded)
- PE: COFF, optional header, sections (name, entropy, characteristics),
  imports, CLR presence, overlay, checksum, RWX / packer heuristics
- ELF: ident, type, machine, interpreter, needed libraries, section names
- Mach-O / fat: magic and CPU only
- ZIP / JAR / APK: entry list, `AndroidManifest.xml`, `classes.dex`,
  native libs, `META-INF` signature presence
- DEX magic

The report is JSON for the tool and a short Markdown projection for the
model. Interesting findings are heuristics (high-entropy sections, RWX,
suspicious imports, missing PE checksum, unsigned APK after a rewrite),
not a CVE feed.

Size cap: 32 MiB. Telegram's own download ceiling is already 20 MiB.

## Artifact mutation

Closed operations only:

```text
write_at     offset + hex bytes
replace_bytes  find hex + replace hex, optional all=true
zip_replace    archive entry name + hex bytes
```

Rules:

- find/replace lengths may differ
- ZIP/APK rewrite re-packs the archive and states that the signature is
  now invalid; Friday does not re-sign
- PE checksum is recomputed when the file is a PE
- result is a new generated document (`*.patched`), delivered through
  the existing `_attachment` / `persist_generated_response_files` path
- refuse if the result would exceed `max_upload_bytes`

This is "change the bytes on the fly", not a compiler.

## Host audit

The operator points at a host. Friday knocks; the operator watches.

Default port set is a short common list (ssh, rdp, smb, http/s,
databases, etc.). The caller may pass up to 64 explicit ports. There is
no 1–65535 sweep in v1.

Each port: TCP connect (2s), optional banner (256 bytes), TLS peer
summary on typical TLS ports, HTTP `HEAD /` on typical HTTP ports.

Always-reject destinations, even when named:

- unspecified / multicast / wildcard
- cloud metadata (`169.254.169.254`, `fd00:ec2::254`)

The owner names the target in chat. That is the targeting authority.

Always-reject, even when named:

- unspecified / multicast / reserved
- cloud metadata (`169.254.169.254`, `fd00:ec2::254`)

There is no CIDR allowlist. A public branch address is valid once the
owner types it.

Hard product boundary:

```text
recon, weakness report, LLM-adversary playbook   — in scope
live exploit payloads / shells / PoCs            — out of scope
```

The operator's stated goal is to test their own defences against an
LLM-driven bot. That is served by: code-owned analysis, allowlisted
knocks, a ranked playbook of what such a bot would try next, and
optional secondary EXTRACT that turns those facts into a sharper
narrative. Friday does not fire exploit payloads. The operator already
said they will watch the wire and can run their own exploits off-box.

A fourth tool `engineer_adversary_rehearsal` is in scope: it restates
the host/artifact findings as an ordered campaign (what an LLM bot
would probe, in what order, which detections should fire) and may
repeat a tiny list of HTTP HEAD/GET paths. It does not send shellcode,
SQLi/RCE strings, or credential-stuffing payloads.

## Secondary brain

Use the existing scheduler. Do not add workloads, do not give it tools,
do not send raw artifact bytes, banners, or extracted strings.

Call shape, identical to Inbox advice:

```text
workload = extract
effect_class = none
require_structured_output = true
contains_private_text = true
priority = background
```

Input is a bounded JSON of hashes, section names, finding codes, open
ports and weakness codes. Output is `{narrative, priorities[]}` or a
fallback to the code-owned Markdown. `assist` only; `shadow`/`disabled`
and `allow_private_text=0` skip. Secret-shaped strings are stripped
before the call so hygiene rejection does not look like a hang.

Reach the scheduler through `ctx.ingestion.secondary_brain`. Do not
add a ServiceContext field and do not edit `friday/secondary_brain/`.

## Files this work may touch

New:

- `outer_sol/ENGINEER_MODE_ARCHITECT_BRIEF.md` (this file)
- `friday/organs/engineer/`
- `tests/test_organs_engineer.py`
- `tests/test_engineer_mode.py`

Surgical:

- `friday/organs/__init__.py` — register the organ
- `friday/storage/_base.py` — `CONVERSATION_MODES` + alias
- `friday/storage/_core.py` — keep `engineer` in mode-repair CASE lists
- `friday/telegram_bridge/_base.py` — `BOT_COMMANDS`
- `friday/telegram_bridge/_commands.py` — `/engineer`, `/help`, `/status`
- `friday/api/conversations.py` — owner gate
- `friday/agent_runtime/__init__.py` — `MODE_GUIDANCE`, budget, skip verifier
- `friday/orchestration/router.py` — legacy-only for this mode
- `tests/test_bridge_surface.py` — advertised commands
- `tests/test_organs_importer.py` — organ inventory
- `docs/ORGANS.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`

## Acceptance

- `/engineer` switches the channel session for the owner and is refused
  for a non-owner
- `/engeneer` is the same switch
- a handmade PE/ELF/ZIP is classified and reports hashes, entropy, and
  format-specific facts without a live LLM
- `replace_bytes` emits different bytes and leaves the source Raw hash
  unchanged
- `127.0.0.1` on a test listener appears open; `8.8.8.8` is refused
  unless allowlisted
- `create_app` still passes `assert_risk_declarations_agree`
- existing dialogue/research Telegram tests stay green
- no change to live production identity or Sol handoff files

## Key decisions

1. **Organ + conversation mode, not a new brain.** The operator asked for
   the same Friday with extra tools and without judges. A second agent
   would be a second personality.
2. **Skip V12 and the answer verifier in this mode only.** Do not disable
   judges globally. Sol's golden journeys stay on dialogue.
3. **Owner-only via empty default presets.** Telegram access piggy-backs
   on the existing owner-chat binding.
4. **Analysis is code-owned.** The model writes the letter, not the PE
   parser. Hostile binaries must not become prompt injection via a
   decompiler dump.
5. **Mutation writes a new file.** Overwriting the uploaded Raw would
   destroy provenance.
6. **Network is recon, not exploitation.** Allowlisted knocks plus a
   report. No payloads. The operator already said they will watch the
   wire themselves.
7. **No version bump.** Concurrent Sol release work owns `CHANGELOG` and
   `__version__`.

## Open questions

None that block v1. Live binary and inter-branch scans are operator-run
after this lands; this brief does not require a golden-journey battery.
