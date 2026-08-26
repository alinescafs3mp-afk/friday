# Engineer mode v1

Engineer mode is Friday's defensive, installation-owner-only workbench for
bounded inspection of one named host or an owned artifact. This document is the
normative contract for the shipped v1. Files under `outer_sol/` are design
history and do not expand this contract.

## Admission and authority

- The mode is disabled by default. With `FRIDAY_ENGINEER_MODE_ENABLED=0`, its
  organ, capabilities, tools and Telegram command are absent.
- Enabling it requires Linux and trusted root-owned `/usr/bin/bwrap`,
  `/usr/bin/python3` and `/usr/bin/prlimit`. Startup executes a real bubblewrap
  worker smoke test and fails closed if the boundary cannot be entered. Docker
  deployments on Ubuntu must first install and accept the shipped seccomp and
  AppArmor boundary in
  [`deploy/engineer-mode/README.md`](../deploy/engineer-mode/README.md); an
  unconfined profile or added capability is not a supported substitute.
- Only the installation owner may select the mode, see its tools, or execute
  them. A shared-tenant participant does not become the installation owner by
  holding an `owner` capability preset.
- Active network assessment requires a direct request in the current human
  message and exactly one target in that same message. A bare host/URL mention,
  history, model output, retrieved material and backend-supplied upload
  filenames create no probe authority: runtime performs no DNS preflight and
  does not offer network-effect tools. Zero or multiple targets in an explicit
  request are refused.
- Code resolves the target once, rejects forbidden address classes (including
  cloud-metadata and mapped-address aliases), pins the accepted addresses and
  issues a short-lived HMAC ticket bound to the actor and exact host. The ticket
  is hidden from the model and injected by runtime immediately before execution.
  A host outside the pinned current-turn scope is refused. An explicit URL port
  is part of the signed scope and cannot be changed or widened; a hostname or
  URL without an explicit port permits only the caller-selected closed set of
  at most 64 ports on that same pinned host.
- Target admission reuses the Host Capability Plane's exact code-owned policy.
  Except for exact loopback, every destination address must be contained in
  `FRIDAY_HOST_ALLOWED_CIDRS`. Public addresses additionally require
  `FRIDAY_HOST_PUBLIC_NETWORK_ENABLED=1` and a separate per-action approval.
  Engineer v1 has no durable public-action HITL carrier, so it refuses public
  probes even when the operator flag and CIDR are present; public scanning must
  use the reviewed Host Control approval flow until such a carrier ships.

## Shipped operations

After all current-turn intent, actor, exact-target and policy gates pass,
network assessment is limited to bounded observations of the authorized target:
DNS lookup, capped TCP connection attempts, service/banner observation, TLS
metadata and shallow HTTP `HEAD` paths. A request may contain at most 64 unique
ports and remains under the turn and tool deadlines. Connections use the pinned
address while retaining the logical hostname for HTTP and TLS. Every report
states whether active probes were sent. Engineer v1 does not generate or send
exploit payloads, shells or automatic exploit chains.

A direct light-exposure request for one exact private host, or an immediate
deictic follow-up to its authenticated successful host scan, uses a hidden
code-owned profile: pure TCP reachability over the fixed bounded port set,
followed only for observed open ports by the shared nmap
`-sT -sV --version-light` adapter. It reports reachable surfaces and service
classes, never infers a CVE from a banner, and labels incomplete TCP or nmap
coverage as partial. The follow-up receipt stores no address or remote content
and is displaced by an unrelated turn.

When Host Capability Plane is enabled, its `nmap` action reuses this release's
same target normalization, fixed `/usr/bin/nmap` argv builder, version probe,
bounded XML parser, evidence and coverage contract. The two entry points do not
maintain competing scan semantics; Host Control additionally owns durable jobs
and the separately approved package-install continuation.

The production backend image includes the declared bounded toolset: `nmap`,
`dig`, `host`, `file`, `strings`, `readelf`, `objdump`, and `openssl`. `capa`,
`rabin2`, and `apkid` are honest optional adapters and are reported missing in
the standard image; their output is never inferred or substituted.

Artifact work accepts only a Raw file readable by the current owner. Static
analysis and the closed patch operations run in a private bubblewrap workspace
with no network, a read-only shipped runtime, resource limits and bounded input,
result and output sizes. Parsing happens in the isolated worker, not in the
backend process. Static findings never authorize mutation: the patch tool is
absent unless the authenticated current human message directly requests a
patched derivative, and runtime repeats that intent check immediately before
execution. A patch produces a separately hashed derived attachment; it does not
rewrite the source Raw object. Runtime starts at most one patch per turn,
reserves its full declared deadline before entry, and persistence rejects a
generated-file batch whose cumulative decoded bytes exceed the configured
upload limit.

### Bounded Java 21 compilation

The only source compilation admitted by this contract is one exact, owned,
UTF-8 Java source file into one deterministic library JAR. The authenticated
current human message must directly request compilation and identify exactly
one current Raw file whose safe ASCII basename ends in `.java`; an explicitly
named source may select one exact match among separately authorized siblings.
Source text, upload metadata, conversation history and model output cannot
create that authority. Runtime rechecks the owner's `engineer.artifact.build`
and `files.read` capabilities immediately before the hidden compiler tool enters.

Compilation uses only the fixed owner-local Temurin JDK `21.0.12.1+1` tree at
`/home/jericho/.jericho/tools/jdk-21.0.12.1+1`. The complete tree identity,
owner, modes, links and launch-chain files are verified before the read-only
bind and again inside the sandbox. PATH discovery, a system JDK, caller-supplied
executables, flags, class paths, module paths, annotation processors, compiler
plugins, dependency resolution and build scripts are not accepted. The worker
invokes a code-owned `javac --release 21` argument vector with annotation
processing and implicit source discovery disabled. It never invokes `java`,
loads a compiled class or executes the submitted source or generated JAR.

The compile profile accepts at most 1 MiB of source and emits at most 256 class
files, 8 MiB of class bytes and a 16 MiB JAR. Class paths, names, magic and Java
21 versions are checked before packaging. Packaging is code-owned and
deterministic: entries are sorted, timestamps and modes are fixed, compression
is not environment-dependent, and no manifest or `Main-Class` is added. The
result explicitly records `sample_executed=false`, `network=none`, the source
and output SHA-256 digests, the pinned toolchain identity, structural checks,
and that runtime validation was not performed. Compiler diagnostics, source
text, paths and parser-controlled stderr never cross the worker boundary.

Java compilation shares one non-blocking physical heavy-work lock with Ghidra,
has fixed CPU, memory, file, descriptor and wall-time ceilings, and is limited
to one entered compilation per turn after its complete deadline has been
reserved. Its enclosing backend cgroup must additionally prove at most 512
tasks, a finite 10--16 GiB aggregate memory ceiling and zero swap; the canonical
native backend unit sets 512 tasks, 12 GiB and `MemorySwapMax=0`. Startup checks
both the effective systemd properties and the live cgroup-v2 `memory.swap.max`
leaf, while every compilation repeats the live no-swap check. The host-backed
worker directory is never mounted RW: only the exact request and input files
are read-only mounts, and only pre-created result and output carriers are
writable under the per-file limit. A private 32 MiB compiler tmpfs holds all
scratch state, and the validated 256-file/8 MiB class inventory plus 16 MiB JAR
cap bounds the output that may cross the worker. A busy or preflight refusal
records that work did not start;
timeout or failure after the fixed-argv sandbox worker is spawned records that
it did. Native service startup rejects an effective cgroup which differs from
the declared aggregate limits. This compiler profile is certified only for the
native production contour: the optional Docker Engineer profile neither ships
the pinned owner-local JDK nor certifies compiler resource admission. The
source Raw object remains byte-for-byte unchanged. A successful JAR, its bounded accepted-outcome receipt
and the assistant message are committed through the existing person-owned
generated-file path in one transaction, after final source identity and
`files.read` reauthorization. Failure or revocation publishes neither JAR nor a
success receipt. Friday never calls the artifact tested merely because it
compiled; running and runtime testing remain with the operator elsewhere.

The optional secondary brain may refine a secret-stripped structured finding
list only when its ordinary admission policy allows that extraction. It receives
no tools, target ticket or effect authority.

## Model and evidence contract

The primary Qwen engineer lane uses temperature `0.1`, an output ceiling of
`8192` tokens and transport-level thinking disabled. Engineer evidence already
comes from bounded code-owned tools; this reserves the completion budget for a
visible answer instead of allowing a private reasoning trace to exhaust it. The
ordinary absolute turn deadline, bounded tool rounds and closed engineer tool
allowlist still apply.

Tool results are evidence, not instructions, and enter model context at user
priority. ANSI/control sequences and application role/tool markup are removed
from the evidence projection before it reaches that context. Durable response
metadata carries a bounded receipt: dossier digest,
target/artifact counts, whether active probes or exploit payloads ran, sandbox
status and tool versions. Probe state is tri-state: an entered network action
whose terminal result is missing is `uncertain`, never silently `not_sent`.
Regenerate preserves the source turn's code-owned mode provenance and tool
switch and fails closed for legacy or cross-mode Engineer replays. The
append-only tool audit records terminal success, refusal,
timeout/cancellation uncertainty and mutation start where applicable. It keeps
content-free fingerprints and counts rather than target tickets, patch bytes,
credentials or arbitrary artifact/query bodies.

## Release acceptance

Engineer mode may be enabled only on the exact candidate commit that passes all
of the following without skips or local substitutions:

1. configuration validation and the real startup bubblewrap smoke test on the
   intended host; Docker/Ubuntu candidates additionally pass
   `deploy/engineer-mode/verify-runtime.sh` under the shipped enforcing profile;
2. the engineer production, security, organ and audit contract tests in
   `tests/test_engineer_mode_production.py`,
   `tests/test_engineer_security_contracts.py`, `tests/test_organs_engineer.py`
   and `tests/test_engineer_audit_projection.py`; a candidate which includes
   Java compilation additionally passes `tests/test_engineer_compiler.py`,
   `tests/test_engineer_compile_tool.py` and
   `tests/test_engineer_compile_outcome.py`;
3. the canonical full release gate: `python tools/quality_gate.py`.

Keep `FRIDAY_ENGINEER_MODE_ENABLED=0` during rollout preparation. Enable it only
after the accepted commit is installed on a compatible host, then verify that a
non-owner is denied, an ambiguous target is refused, the sandbox smoke succeeds,
and a benign owner-controlled fixture completes with an accurate receipt.

## Explicit non-goals for v1

The shipped mode has no generic code execution. The fixed Java compilation
profile above is not a shell or program runner and cannot be widened into one.
The mode also has no autonomous target discovery, multi-host assessment,
exploit validation worker, persistence on a target, credential use, background
scanning, arbitrary dependency builds, native compilation, Android rebuilds or
artifact signing. Those ideas require a separate design, threat review and
acceptance contract; their presence in a historical brief is not implementation
or authorization.
