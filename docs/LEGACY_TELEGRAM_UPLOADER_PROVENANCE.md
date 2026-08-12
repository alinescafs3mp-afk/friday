# Exact legacy Telegram uploader provenance

`tools/backfill_legacy_telegram_uploader_provenance.py` is an offline,
fail-closed maintenance tool for live Telegram file Raw Objects that never
received an `uploaded_by` key. It fills **only** rows whose bounded metadata
already carries a Telegram chat identity that maps **1:1** under closed
authority rules.

## Archive authority

- Shared owner archive only: `--tenant-id` **must equal** `--owner-id`, and both
  must be the exact `LEGACY_OWNER_USER_ID` string. That users row must exist
  once with `status='active'` and `preset_key='owner'`. Other active
  owner-preset accounts are allowed and **ignored** by this gate (the tool never
  scans the owner-preset population or picks “first”). A non-canonical tenant,
  a missing/inactive/non-owner canonical row, or tenant≠owner fails closed.
  Owner A cannot stamp Raw rows of tenant B. The same gate runs on preflight
  plan and again on the in-transaction replan.
- Primary mapping class `identity_current`:
  `user_identities(source='telegram', external_id=chat_id)` → exactly one
  active user, plus exactly one **owned unarchived** Telegram
  `channel_sessions` row for that chat whose `conversation_id` joins
  `conversations` with `c.user_id = s.user_id` and `c.is_archived=0`.
- Closed fallback class `legacy_external_current` only when **no** identity row
  exists: exactly one active `users` row with `source='telegram'` and
  `external_id=chat_id`, plus the same owned unarchived session proof. Classes
  are never mixed.
- Stale/archived/mismatched conversation owner/duplicate sessions or identities
  → ambiguous or unmapped (zero-write).

It does **not**:

- assign ownership to the archive operator/owner in bulk;
- invent identity from filename, path, content, latest message, or model;
- create `file_source_aliases`;
- mutate CLI imports, private/ignored/deleted rows, explicit
  `uploaded_by: null`, existing uploaders, ambiguous/unmapped chats, or
  invalid/legacy/disk-failed registrations.
- grant document-read authority: exact audio/voice attribution is
  **metadata-only** in this offline tool. Runtime document readers still
  exclude audio/voice carriers.

## Preview (read-only)

Friday writers should be stopped for a stable inventory. Default mode never
writes:

```bash
.venv/bin/python tools/backfill_legacy_telegram_uploader_provenance.py \
  --database /absolute/path/to/jericho.sqlite3 \
  --files-root /absolute/path/to/data/files \
  --tenant-id CANONICAL_OWNER_ID \
  --owner-id CANONICAL_OWNER_ID \
  --report /private/path/legacy-tg-uploader-preview.json
```

Stdout and the private report (mode `0600`) contain only counts, classes, short
tags, and `plan_sha256`. Full Raw/user/chat/path/hash identities stay in the
private plan basis and are never printed.

## Apply (explicit contract)

Apply requires **all** of:

1. private claim manifest mode `0600`, `approved: true`, exact schema/scope;
2. `--expect-count` equal to the current plan;
3. `--expect-plan-sha256` equal to the current plan SHA;
4. verified full DB backup under `--backup-dir`;
5. `--i-confirm-writers-stopped` (stopped-writers acknowledgement);
6. canonical archive owner (`LEGACY_OWNER_USER_ID`, `preset_key=owner`,
   `status=active`) separate from the mapped uploaders written into metadata.
   Claim schema is `…claim.v2`; a v1 manifest is refused.

Claim manifest shape (counts come from the current plan; do not reuse a
remembered integer):

```json
{
  "approved": true,
  "candidate_count": "<integer from the current plan>",
  "claim_scope": "exact_legacy_telegram_uploader_mappings",
  "owner_id": "CANONICAL_OWNER_ID",
  "plan_sha256": "COPY_THE_64_HEX_PREVIEW_VALUE",
  "schema": "friday.legacy-telegram-uploader-provenance-claim.v2",
  "tenant_id": "CANONICAL_OWNER_ID"
}
```

```bash
.venv/bin/python tools/backfill_legacy_telegram_uploader_provenance.py \
  --database /absolute/path/to/jericho.sqlite3 \
  --files-root /absolute/path/to/data/files \
  --tenant-id CANONICAL_OWNER_ID \
  --owner-id CANONICAL_OWNER_ID \
  --apply \
  --claim-manifest /private/path/claim.json \
  --expect-count COPY_THE_CURRENT_PLAN_CANDIDATE_COUNT \
  --expect-plan-sha256 COPY_THE_64_HEX_PREVIEW_VALUE \
  --backup-dir /private/path/backups \
  --i-confirm-writers-stopped \
  --report /private/path/legacy-tg-uploader-applied.json
```

There is no force mode and no all-users default. Apply takes
`BEGIN IMMEDIATE` (via `FridayStorage.transaction`), recomputes the plan,
requires exact equality, performs per-row metadata compare-and-swap adding only
`uploaded_by=<exact mapped user id>`, writes one allowlisted
`cli.legacy_telegram_uploader.backfill` audit row per Raw in the same
transaction, republishes privacy derivatives, and checks integrity/FK plus a
zero remaining plan before commit. Any error before commit rolls back both
metadata and audit. Post-commit integrity and derivative validity are checked
again.

## Eligibility (exact)

A Raw is planned only when **all** hold:

1. metadata is bounded valid object JSON and does **not** contain the key
   `uploaded_by` (explicit `null` is refused);
2. no non-empty `import_source_path` (CLI imports are out of scope);
3. `channel` ∈ {`telegram`, `telegram-bridge`, `api-token`} and `chat_id` is a
   positive decimal ASCII identity;
4. closed exact mapping (`identity_current` or `legacy_external_current` only);
5. owned unarchived session JOIN as above;
6. tenant is the exact shared canonical owner archive
   (`tenant_id == owner_id == LEGACY_OWNER_USER_ID`), `source='upload'`,
   `content_type='file'`, live, public/non-private, not ignored in inbox;
7. registration is modern-valid and registered bytes under `--files-root`
   verify size and SHA via the shared verifier.

Ambiguous, unmapped, mismatched, existing uploader, explicit null, CLI import,
private, ignored, deleted, invalid/legacy registration, and disk hash/size
mismatch remain zero-write. Exact audio/voice carriers **may** be planned by
this offline tool (`planned_audio` on the private plan/report); that write is
metadata `uploaded_by` plus audit only. Document readers keep the audio veto.

Private plan basis (v3) includes mapping evidence class, conversation/session
identity material, canonical-owner gates, and per-candidate `audio_carrier` so
an authority or audio-class change changes `plan_sha256`. Basis values are
never printed in public reports.
