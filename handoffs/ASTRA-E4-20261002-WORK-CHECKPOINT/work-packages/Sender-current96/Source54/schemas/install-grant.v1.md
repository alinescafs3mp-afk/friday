# Install grant v3 (historical v1 filename)

Wire schema is `friday.install-grant.v3`. Versions 1 and 2, added or missing keys,
and a version-1 install/remove effect bill are rejected; there is no automatic
migration or downgrade. This is a private proposed contract, not owner approval
of root effects. Production material authority remains FORMAT_NOT_COVERED and
production root/live/release proof remains NOT_PROVEN.

Encoding: canonical ASCII JSON, sort_keys=True, compact separators, allow_nan=False and one LF. Duplicate/unknown/missing keys and noncanonical round trips fail. External expected SHA256 is mandatory for authority documents; local/staged/self manifests never supply trust. Lowercase 64-hex digests, exact bools versus ints, bounded paths/counts/depth/sizes. Every nested record is exact-key checked by the named source parser. Source constants define the closed executable field inventory; source-review-index binds their symbols and tests.

Parser: install_bootstrap.parse_install_grant; InstallPlan.from_documents binds every input. Grant requires external expected digest, candidate/generation/transaction, package/provenance/manifest/effect bill, exact source/destination member hashes and ownership/modes, fixed broker/interpreter/policy and stable install identity. No caller-selected arbitrary installed paths. Real root requires EUID0/root '/', non-root recording fake mode requires exact explicit test token and private non-'/' root; root+fake and nonroot+real fail before writes. Each generated externally visible effect is grant-authorised and tested. A root-install grant never supplies a live grant. Immutable bootstrap trust is copied from a separately externally approved grant-bound record.

Exact top-level keys: schema,transaction_id,candidate_commit,candidate_tree,attempt_generation,authority_id,caller_uid,package_index_sha256,snapshot_manifest_sha256,provenance_sha256,assembly_recipe_sha256,creation_tool_sha256,owner_approval_sha256,approved_authorities,bootstrap_python_path,bootstrap_python_sha256,source_root_identity,members,authority,bootstrap_trust,effect_bill,effect_bill_sha256,policy_sha256,previous_removed_journal_sha256,retained_directories,snapshot_directories,snapshot_envelope. The predecessor field is an external pin for a new transaction following authenticated removed state; retired IDs cannot resume. Exact member keys: source,destination,type,size,sha256,target,uid,gid,mode,source_identity. Exact filesystem identity keys: dev,ino,mount,uid,gid,mode,nlink,type. Immutable authority/bootstrap-trust templates contain only specified zero-digest placeholders needed to avoid hash cycles; deterministic resolution is bound to the authenticated grant, never learned from staged bytes.

V3 adds mandatory `retained_directories`, a bounded sorted unique list of exact
`path,identity` records. It is empty for an initial transaction. Each identity
is a root-owned directory with the strict identity keys above; each path must
belong to the request's nonbootstrap namespace-mkdir graph. A nonempty inventory
requires the external `previous_removed_journal_sha256` pin. Before any new
journal publication, the installer authenticates that exact removed predecessor
and checks the inventory equals its surviving reusable journaled directories
and that each current held/named identity still matches. Only the matching
namespace-mkdir effect may reuse it. Unknown existing objects, changed identities,
foreign paths, missing entries, missing predecessor, and retired transactions
refuse without payload publication or deletion. Current/staged bytes cannot
invent this authority; the v3 grant and predecessor digest are externally pinned.

V2 added the mandatory `snapshot_directories` and `snapshot_envelope` top-level
keys. Directory records have exactly path,uid,gid,mode and must equal the pinned
manifest directories (root:root 0555). Envelope records have exactly
name,size,sha256,uid,gid,mode: the sorted pair manifest.v1.json and
provenance.v1.json, root:root 0444, one link, grant-bound sizes and digests.
Scratch root:root 0700 and the canonical root:root 0600 live lock are explicit
runtime prerequisites. A separate externally pinned revoke-remove v2 document
must bind this exact install identity and grant and its exact effect bill; only
the exact sorted live grant/pin/effects triplet can be additionally authorized.

Nested authority, signer, public key and bootstrap trust records are parsed by
their strict source parsers. Every digest (including the grant itself) requires
an independent external pin. Integer fields reject booleans/floats; environment
and sys_path are exact string maps/lists, allowed_fds is exactly [0,1,2]. The
effect bill is version 2 with exact semantic allowed/forbidden/scope/authority
sets; narrowed, unrelated or expanded sets do not grant a capability.

ApprovedMaterials is retained as an issued sealed object in InstallPlan and
revalidated at root-install capability creation. Callback fixtures are always
fixture-tainted; editing a mode label or dataclass replacement cannot elevate
them to native authority. The fake backend selects an explicit inert syscall
capability; actual FilesystemBackend methods remain shared and tested.
