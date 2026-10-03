# installed-authority.v1

Encoding: canonical ASCII JSON, sort_keys=True, compact separators, allow_nan=False and one LF. Duplicate/unknown/missing keys and noncanonical round trips fail. External expected SHA256 is mandatory for authority documents; local/staged/self manifests never supply trust. Lowercase 64-hex digests, exact bools versus ints, bounded paths/counts/depth/sizes. Every nested record is exact-key checked by the named source parser. Source constants define the closed executable field inventory; source-review-index binds their symbols and tests.

Parser: broker_runtime.parse_installed_authority. Exact keys: schema,authority_id,candidate_commit,candidate_tree,attempt_generation,package_index_sha256,broker_bundle_sha256,bootstrap_python_path,bootstrap_python_sha256,snapshot_root,snapshot_manifest_sha256,provenance_sha256,candidate_controller_sha256,gate_argv_sha256,environment_sha256,caller_uid,gate_uid,gate_gid,sudoers_path,sudoers_sha256,ledger_directory,runtime_journal_path,live_lock_path,scratch_parent,evidence_parent,install_grant_sha256,install_identity_sha256. Fixed absolute canonical namespaces and digest-specific snapshot/broker paths only; no current/cache/retention authority. Final install identity is the immutable transaction projection, never phase journal digest. Authority itself and bootstrap transport trust require external expected digests.

The installed authority v1 record is unchanged but is issued only from the
strict install grant/journal v2 transaction. The installed binding requires
installed_not_live with all closed graph obligations; a separate staged binding
is restricted to installer verification and is not runtime authority. Runtime
envelope files, scratch owner/mode and the canonical live-lock identity are
verified prerequisites. A root-install capability never supplies live authority.
The exact grant/pin/effects documents for live authority require separate pins
and their separate remove grant agreement. Protected interpreter startup and
authentic production material receipts remain NOT_PROVEN; runtime GO refusal
is unconditional. Inert fixture success is not owner approval or root/live proof.
