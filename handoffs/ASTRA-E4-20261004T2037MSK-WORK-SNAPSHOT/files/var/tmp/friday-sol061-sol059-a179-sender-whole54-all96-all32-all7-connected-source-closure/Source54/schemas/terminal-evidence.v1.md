# terminal-evidence.v1

Encoding: canonical ASCII JSON, sort_keys=True, compact separators, allow_nan=False and one LF. Duplicate/unknown/missing keys and noncanonical round trips fail. External expected SHA256 is mandatory for authority documents; local/staged/self manifests never supply trust. Lowercase 64-hex digests, exact bools versus ints, bounded paths/counts/depth/sizes. Every nested record is exact-key checked by the named source parser. Source constants define the closed executable field inventory; source-review-index binds their symbols and tests.

Parser: custody_linux.LinuxCustodyBackend.validate_evidence. Exact evidence keys: schema,attempt_id,verdict,exit_code,stdout_sha256,stderr_sha256,startup,artifacts. Candidate/generation/authority/grant/consumed/snapshot context is authenticated by the enclosing consumed ledger, runtime journal and fixed grant; those fields are not silently invented in this evidence object. Startup projection and exact artifact rows are validated against the grant. Signal, timeout, nonzero exit, extra/missing output, digest or publication ambiguity results in failure. Root supervisor retains publication descriptors/lock; no-replace final publication follows bounded stream capture, exact member validation and fsync. PASS requires exit0, canonical PASS evidence and complete durable terminal commit with no failure veto; the journal alone never grants PASS.

The production post-exec hostile-same-UID boundary is unresolved and LinuxCustodyBackend.start_child refuses before GO. Modeled same-UID substitution, mount/descriptor/signal/timeout/evidence/publication tests are protocol observations, not proof of live kernel custody. Terminal evidence and permanent consumed ledger are outside every removal capability and survive removal. Private test retained records are observations only.

exit_code is an exact JSON integer in [0,255]; PASS requires integer0. false and 0.0 refuse, including when the independently observed child status is0. Artifact records have exact path/size/sha256 keys, sorted unique canonical paths, exact bounded integer sizes and strict digests. Every needed nested final directory is sealed root:evidence-group0750 and fsynced; regular files are root:evidence-group0440. Recording adapters model those owner/mode syscalls and cannot prove host group traversal. post:custody:fixed_exec denotes the supervisor's observed successful worker-completion boundary; ordinary exec has no returning post hook and this point does not prove post-exec isolation.

SOL020 author-component evidence is not production terminal authority. Each of the six focused roles (foundation, install, runtime, foundation-handoff, harness-trust, harness-graph) uses friday.sol020.focused-component.v1 with exactly schema, assignment, generation, lane, package_root, final_source_hashes, coverage, focused_revalidation, limitations, source_package_accepted, GO, independent_acceptance. The last three fields are exact false. Full final source/harness/config inventory must equal the authenticated current inventory, not a selected hash subset. focused_revalidation has exact status/evidence_ref/evidence_sha256/required_method_ids/observed_method_ids/missing_method_ids/historical_basis_sha256 keys. The consumer validates a fresh focused execution graph before crosslinking exact methods; historical reports and hashes alone cannot grant revalidation. Both harness roles require an actually executed and strictly authenticated harness_selfcheck.main. Missing execution yields PARTIAL_NOT_RUN, never manufactured approval.

friday.sol020.integration-review.v1 binds the exact six component hashes, current entire source inventory, fixed all30 finding IDs and original findings digest, and the same strict focused evidence reference/hash. A readiness boolean never replaces receipt validation. Strict key-specific owner/outcome pairs and matrix owners are declared before execution and consumed from the independent collection. Valid-but-wrong PASS/REFUSED/CRASH_RETAINED outcomes refuse if not the declared exact pair. Source-control diagnostic revalidation is separate from two entire official final passes; no incomplete worker or older snapshot earns official credit. Independent source/integration/final acceptance remains Astra's.
# SOL020 exact authority delta

Current coverage must equal the strictly typed structural projection of each
initially authenticated named historical author contract. Historical hashes alone
cannot credit substituted coverage or execution. Every required key needs its
current successful declared method and exact source-declared outcome.
Integration readiness binds the independently validated complete official pair
reference and SHA, separately from partial focused evidence. Preservation requires
the exact preparation child set, accepted results, confirmed archived closed
trees and their actual hashes, and an exact named-old-pin proof rechecked now.
These predicates never constitute final independent acceptance or GO authority.

# A049 private receipt authority delta

The SOL020 author schemas and evidence above remain inherited data only.
Current recording wires use friday.a049.*. Each current component adds exact
run_context and evidence_origin=FRESH_CURRENT_RUN; integration adds the same exact
fields. The run_context equals the separately issuer-authenticated immutable RUN
binding used by actual dispatcher/worker/selfcheck receipts and raw/semantic unions.
A current component is validated only after the fresh focused launch graph,
actual successful method owners, mandatory exact key outcomes and entire mandatory
source/harness/config/schema digests authenticate. The same exact G2/G3 rows are
added by fresh_coverage_contract; old original rows remain intact.
No old execution, source hash relabel, field/status change or current timestamp can
replace current method evidence, independent review or two full final passes.

The source-declared schemas/control-inventory.v1.json contains the exact464/657/
374/11819 expected identity unions. Actual collection must match its complete
selected-module projection before ordinary tests run; all old subsets are retained.
The finite isolated56-case plan is in schemas/affected-check-plan.v1.json.
Neither file is an observed runtime result or runtime collection PASS.

All30 public original-obligation statuses remain exactly UNRESOLVED until separately actual independent final acceptance. Each source-review finding has a separate typed source_control_evidence_assessment=UNRESOLVED|PARTIAL|SOURCE_CONTROLLED; terminal all30_source_control_evidence_assessments preserves this assessment independently of all30_statuses. Complete mappings/passes cannot promote original status; independent_acceptance remains false.

Current run_binding additionally carries exact parent_custody; see run-context.v1.md. No original acceptance writer is added.
