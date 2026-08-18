# Qwen3.8 V12 attested sibling bundle

This directory is a fail-closed deployment bundle for the exact graph-only
Qwen3.8 dispatcher.  It does not alter the stable deployment.  The base compose
file has no host `ports`; port 8001 exists only in the separate publish overlay
and must be used exclusively by the transactional GO switch.

## Immutable identities

- Engine image ID: `sha256:7f27e2885eca5041860a8c28c0bc3304b43b9fce072f298da043393866aa5887`
- Proxy image ID: `sha256:2bf585895ba4ede01899f4b17db5c690dd893d77c3e1da9ac4dfb2482e22c091`
- Model manifest semantic SHA-256: `da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333`
- Launch manifest semantic SHA-256: `640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b`
- Proxy policy raw-file SHA-256: `47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae`

The two semantic hashes are SHA-256 over UTF-8 JSON serialized with sorted
keys, no insignificant whitespace, `ensure_ascii=false`, and forbidden NaN or
Infinity.  Array order remains significant.  The proxy policy hash is over the
exact bytes of `default.conf.template`.

`build-attestation.v1.json` records the remote build result and immutable input
hashes.  The builder verified exact base image IDs, base filesystem layer
prefixes, and code-owned OCI labels.  It created images only; no candidate
container was created or started.

## Runtime witness contract

On every engine process start, the wrapper first removes any stale witness,
rehashes the exact 17-file, 21,952,105,742-byte read-only model snapshot, chooses
a fresh 256-bit nonce and SGLang random seed, atomically writes the witness, then
executes the exact launch manifest.  The proxy exposes it only as authenticated
`GET /_friday/v1/deployment-witness` and allowlists only health, chat, models,
metrics, and server-info routes.  All update/admin routes fall through to 404.

The engine never mounts the mutable host snapshot.  Before stable mutation, the
switch copies and hashes the host source into the new, exact Docker volume
`jarvis-gpt-qwen38-v12-attested-model-da435c4b7556d8d5`, verifies that volume in
a separate read-only pass, and mounts it read-only at the unchanged model path.
An existing partial or wrongly labelled volume is rejected and never overwritten.

This attests drift and restarts within the trusted Docker launcher boundary.  It
does not claim protection against a hostile Docker/host administrator.
