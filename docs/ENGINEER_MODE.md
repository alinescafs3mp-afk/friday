# Engineer mode v1

Engineer mode is Friday's defensive, installation-owner-only workbench for
bounded inspection of one named host or an owned artifact. This document is the
normative contract for the shipped v1. Files under `outer_sol/` are design
history and do not expand this contract.

## Admission and authority

- The mode is disabled by default. With `FRIDAY_ENGINEER_MODE_ENABLED=0`, its
  organ, capabilities, tools and Telegram command are absent.
- Enabling it requires Linux and trusted root-owned `/usr/bin/bwrap` and
  `/usr/bin/python3`. Startup executes a real bubblewrap worker smoke test and
  fails closed if the boundary cannot be entered.
- Only the installation owner may select the mode, see its tools, or execute
  them. A shared-tenant participant does not become the installation owner by
  holding an `owner` capability preset.
- A network turn must contain exactly one target named by the current human
  message. History, model output, retrieved material and backend-supplied upload
  filenames cannot create or widen target authority. Zero or multiple targets
  are refused.
- Code resolves the target once, rejects forbidden address classes (including
  cloud-metadata and mapped-address aliases), pins the accepted addresses and
  issues a short-lived HMAC ticket bound to the actor and exact host. The ticket
  is hidden from the model and injected by runtime immediately before execution.
  A host outside the pinned current-turn scope is refused; a URL port is only a
  starting hint, while any caller-selected ports remain subject to the closed
  64-port cap and the same exact-host authority.

## Shipped operations

Network assessment is limited to bounded observations of the authorized target:
DNS lookup, capped TCP connection attempts, service/banner observation, TLS
metadata and shallow HTTP `HEAD` paths. A request may contain at most 64 unique
ports and remains under the turn and tool deadlines. Connections use the pinned
address while retaining the logical hostname for HTTP and TLS. Every report
states whether active probes were sent. Engineer v1 does not generate or send
exploit payloads, shells or automatic exploit chains.

Artifact work accepts only a Raw file readable by the current owner. Static
analysis and the closed patch operations run in a private bubblewrap workspace
with no network, a read-only shipped runtime, resource limits and bounded input,
result and output sizes. Parsing happens in the isolated worker, not in the
backend process. A patch produces a separately hashed derived attachment; it
does not rewrite the source Raw object. Runtime starts at most one patch per
turn, reserves its full declared deadline before entry, and persistence rejects
a generated-file batch whose cumulative decoded bytes exceed the configured
upload limit.

The optional secondary brain may refine a secret-stripped structured finding
list only when its ordinary admission policy allows that extraction. It receives
no tools, target ticket or effect authority.

## Model and evidence contract

The primary Qwen engineer lane uses temperature `0.1`, an output ceiling of
`8192` tokens and model thinking when the real transport supports it. Thinking
is transport-private and removed from the user-visible answer. The ordinary
absolute turn deadline, bounded tool rounds and closed engineer tool allowlist
still apply.

Tool results are evidence, not instructions, and enter model context at user
priority. Durable response metadata carries a bounded receipt: dossier digest,
target/artifact counts, whether active probes or exploit payloads ran, sandbox
status and tool versions. Regenerate preserves the source turn's code-owned
tool switch and fails closed for legacy Engineer turns without that marker. The
append-only tool audit records terminal success, refusal,
timeout/cancellation uncertainty and mutation start where applicable. It keeps
content-free fingerprints and counts rather than target tickets, patch bytes,
credentials or arbitrary artifact/query bodies.

## Release acceptance

Engineer mode may be enabled only on the exact candidate commit that passes all
of the following without skips or local substitutions:

1. configuration validation and the real startup bubblewrap smoke test on the
   intended host;
2. the engineer production, security, organ and audit contract tests in
   `tests/test_engineer_mode_production.py`,
   `tests/test_engineer_security_contracts.py`, `tests/test_organs_engineer.py`
   and `tests/test_engineer_audit_projection.py`;
3. the canonical full release gate: `python tools/quality_gate.py`.

Keep `FRIDAY_ENGINEER_MODE_ENABLED=0` during rollout preparation. Enable it only
after the accepted commit is installed on a compatible host, then verify that a
non-owner is denied, an ambiguous target is refused, the sandbox smoke succeeds,
and a benign owner-controlled fixture completes with an accurate receipt.

## Explicit non-goals for v1

The shipped mode has no generic code execution, autonomous target discovery,
multi-host assessment, exploit validation worker, persistence on a target,
credential use, or background scanning. Those ideas require a separate design,
threat review and acceptance contract; their presence in a historical brief is
not implementation or authorization.
