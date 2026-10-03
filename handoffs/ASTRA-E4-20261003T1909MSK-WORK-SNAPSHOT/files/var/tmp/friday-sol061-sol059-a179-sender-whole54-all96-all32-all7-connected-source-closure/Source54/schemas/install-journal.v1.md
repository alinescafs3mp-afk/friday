# Install journal v2 (historical v1 filename)

Wire schema is `friday.install-journal.v2`; v1 journals, missing/extra keys and
downgrades refuse. There is no migration that infers new authority from old
staged bytes. Root/live/material/release proof is NOT_PROVEN.

Encoding: canonical ASCII JSON, sort_keys=True, compact separators, allow_nan=False and one LF. Duplicate/unknown/missing keys and noncanonical round trips fail. External expected SHA256 is mandatory for authority documents; local/staged/self manifests never supply trust. Lowercase 64-hex digests, exact bools versus ints, bounded paths/counts/depth/sizes. Every nested record is exact-key checked by the named source parser. Source constants define the closed executable field inventory; source-review-index binds their symbols and tests.

Parser: install.InstallJournal. Canonical hash-chained exact identity/generation/phase/effect records; previous_journal_sha256 links successors, journal_sha256 covers its projection. Initial journal is durable before payloads. Full INSTALL_PHASES and REMOVE_PHASES are exported in source and fault-tested before/after stage write and publication. Staged successor must be exact valid identity and successor; torn/corrupt/foreign/old/third-transaction states refuse. Install policy is last after reauthentication. Removal is one-way; policy is revoked first, grant next, execution fenced, exact bytes removed. Recovery may not republish after revocation. Removed journal/install lock and permanent attempt/evidence are retained.

Exact journal keys: schema,transaction_id,install_identity_sha256,identity,generation,phase,previous_journal_sha256,journal_sha256,retired_transaction_ids,records,objects,completed_effects. Each hash-authenticated delta record has exact keys generation,phase,previous_journal_sha256,record_sha256,objects,completed_effects. The head binds the final record hash, which authenticates its predecessor chain; every delta is parsed and reconstructed against exact identity, forward transitions and effect inventory. Install phases: install_prepared,payloads_staged,snapshot_publishing,snapshot_published,broker_publishing,broker_published,authority_publishing,authority_published,policy_publishing,policy_published,installed_not_live. Remove phases: remove_prepared,policy_revoking,policy_revoked,live_grant_removing,authority_removing,snapshot_removing,broker_removing,private_stages_removing,removed. Phase/effect controls and their actual coverage are terminally mapped by source-review-index.json.

V2 adds mandatory head `pending_effect`; each record adds `pending_effect` and
`cancelled_effect`. The pending intent is null or exactly effect,add,remove,moved,
predecessors. It is durably journaled before the syscall. A matching completion
clears the intent and may change only the closed effect's granted objects;
cancellation clears an unapplied intent without claiming its effect completed.
The final reconstructed head must exactly equal the record chain. Effect IDs,
object paths, phase obligations and predecessor dependencies come from
effect_phase_map/effect_object_contract/effect_dependencies; unknown effects,
wrong phase, inode adoption, duplicate completion, foreign paths and terminal
phases missing their obligations all refuse even with coherent rewritten hashes.

The immutable identity now contains a strict v3 install grant. Rollover reuses
only its externally pinned `retained_directories`, after authenticating the exact
removed predecessor named by that grant and comparing its surviving namespace
directory inventory with current held/named identities. The new journal starts
with no adopted objects; each reused directory is durably introduced by its
exact namespace-mkdir intent and completion. Receipt parsing requires that
preexisting namespace predecessor to equal the grant's exact retained identity.
No unknown current directory or inferred hash supplies reuse authority.

Every nonbootstrap effect exports distinct pre/post intent-stage and
intent-publication, pre-effect, applied-after-syscall, pre/post completion-stage
and completion-publication, and post-effect intervals. Both journal validation
and any staged promotion require the exclusive transaction lock first. Every
fresh destructive removal suffix reacquires the live lock; recorded booleans
never recover process-local lock authority. Publication/removal retain opened
objects and full ancestor leases and compare named/opened identities before
and after the syscall and parent fsync.

A create syscall followed by death before its new inode identity is durably
recorded cannot be authenticated by the intent alone. Recovery preserves that
object and refuses PENDING_CREATE_IDENTITY_NOT_PROVEN; it never adopts or deletes
the ambiguous object. Thus exhaustive bounded modeled-prefix observations may
show safe incomplete recovery, not atomic install completion or native crash
durability proof. Exact selected raw journal/stage bytes, identity graphs and
ordered actual hooks are retained by the recording controls. Modeled prefix
crashes and directly thrown exceptions are labeled separately.
# SOL020 pending regular-member copy

The fixed empty predecessor follows the granted create operation's absent-to-
journal-owned inode history and the required create dependency. A copy intent
must carry that exact non-null journal-owned predecessor. A new intent checks
stable empty bytes before writing intent records. Recovery verifies complete
unchanged predecessor identity around its byte reads: empty for a nonempty
grant means unapplied/replay without claiming completion; exact granted bytes
mean applied; partial or substituted nonempty bytes refuse and are preserved.
An empty granted payload is a byte no-op. No wire keys are added and no arbitrary
observed content hash becomes authority. Existing matrices/row owners/outcomes
remain required. Native durability and create-to-durable identity stay NOT_PROVEN.

# A049 mandatory semantic lineage and direct-removal declarations

The original effect/phase/object schema and dependencies remain unchanged.
Two additional strict parser/binding controls isolate absent journal-owned object
and missing required member_create completion/dependency on coherent owned inert
chain data. The unchanged installed positive and exact semantic refusal reasons
are mandatory, separately method-owned source declarations.

Direct Remover.remove controls begin from authentic pending-copy prefixes and
a fresh separate revoke-remove fixture capability. The exact12-state catalogue
and row owners are declared in install_controls.remove_contract before execution.
Unapplied copy cancellation differs from completed copy; ambiguity preserves
object/journal bytes. Ordered actual consumers, revoke-first/fence/permanent records
are retained. Missing-row/wrong-owner/wrong-valid-enum receipt controls are required.
No native or installation/removal effect was executed during source preparation.
