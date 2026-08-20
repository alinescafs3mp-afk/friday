# Qwen3.8 V12 attested sibling bundle

This directory is a fail-closed deployment bundle for the exact graph-only
Qwen3.8 dispatcher.  It does not alter the stable deployment.  The base compose
file has no host `ports`; port 8001 exists only in the separate publish overlay
and must be used exclusively by the transactional GO switch.

The base graph remains deliberately internal: the engine has exactly one
attachment, `jarvis-gpt-qwen38-v12-attested-net` (`bridge`, `internal=true`),
and the unpublished proxy has that same single attachment. The publish overlay
adds only the proxy to the durable code-owned
`jarvis-gpt-qwen38-v12-attested-publish-net` (`bridge`, `internal=false`,
`attachable=false`). Its `gw_priority=1` makes that endpoint the proxy's default
gateway; the internal endpoint remains priority 0. The engine is never attached
to the publish bridge and therefore never gains its host-facing gateway.

This split is required by Docker Desktop. A container whose only network is
internal can retain an exact `HostConfig.PortBindings` entry while Desktop
cannot select a reachable container IP, so no host publisher is created. The
dedicated bridge gives Desktop one eligible proxy IP without weakening the
engine network or the authenticated same-origin proxy/witness boundary.

## Immutable identities

- Engine image ID: `sha256:4a38144134d84d6f78c1844314f209c48ef69c4bd8bf7da1e5c400f9abda6f26`
- Proxy image ID: `sha256:37ae13a39a5d8a0780b0b0f226065753c0d929c31956be27f7f375f79cdef750`
- Model manifest semantic SHA-256: `da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333`
- Launch manifest semantic SHA-256: `640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b`
- Proxy policy raw-file SHA-256: `d51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe`

The two semantic hashes are SHA-256 over UTF-8 JSON serialized with sorted
keys, no insignificant whitespace, `ensure_ascii=false`, and forbidden NaN or
Infinity.  Array order remains significant.  The proxy policy hash is over the
exact bytes of `default.conf.template`.

`build-attestation.v1.json` records the remote build result and immutable input
hashes.  The builder verified exact base image IDs, base filesystem layer
prefixes, and code-owned OCI labels.  It created images only; no candidate
container was created or started.

`Test-AttestedProxy.ps1` reads the current key only from the exact running
stable proxy, never prints it, and verifies both a 256-character key and that
stable key with `nginx -t`. Its isolated, unpublished container then proves
exact-key access, case-flipped/missing/wrong-key rejection, and management-route
closure before any deployment.

`ORCHESTRATION.md` contains the exact frozen Linux-to-Windows transport and
execution order. Its code-owned sync wrapper pins both SSH identities and every
old/new file hash, defaults to a no-network local plan, and separates the
three-file cleanup bootstrap from the complete post-cleanup sync.

## Runtime witness contract

On every engine process start, the wrapper first removes any stale witness,
rehashes the exact 17-file, 21,952,105,742-byte read-only model snapshot, chooses
a fresh 256-bit nonce and SGLang random seed, atomically writes the witness, then
executes the exact launch manifest.  The proxy exposes it only as authenticated
`GET /_friday/v1/deployment-witness` and allowlists only health, chat, models,
metrics, and server-info routes. Authorization uses an ordinal, case-sensitive
comparison against the full `Bearer ` value; missing, wrong, or case-flipped
keys return 401. All update/admin routes fall through to 404.

The engine never mounts the mutable host snapshot.  Before stable mutation, the
switch copies and hashes the host source into the new, exact Docker volume
`jarvis-gpt-qwen38-v12-attested-model-da435c4b7556d8d5`, verifies that volume in
a separate read-only pass, and mounts it read-only at the unchanged model path.
An existing partial or wrongly labelled volume is rejected and never overwritten.

This attests drift and restarts within the trusted Docker launcher boundary.  It
does not claim protection against a hostile Docker/host administrator.
