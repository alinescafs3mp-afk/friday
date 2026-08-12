# Telegram file alias backfill (private-safe runbook)

Tool: `tools/backfill_telegram_file_aliases.py`

Scope: recover **telegram-file** aliases from immutable Raw `source_ref` only.

Does **not** invent `telegram-message` / `telegram-unique` without durable evidence.

## Identity split

| CLI flag | Meaning |
|----------|---------|
| `--tenant-id` | Shared archive tenant (`raw_objects.user_id`) |
| `--owner-id` | Active **operator-owner** who approves the claim (`preset_key=owner`) |
| `--uploader-id` | Exact target uploader scope (owner **or** another active user, e.g. JBL) |
| `--files-root` | Configured files directory (required; shared `verify_registered_file_bytes`) |

Plan is always **exact-uploader scoped**. There is no “all uploaders” default.

Claim manifest includes **both** `owner_id` and `uploader_id`.

## Dry-run (default)

```bash
.venv/bin/python tools/backfill_telegram_file_aliases.py \
  --database "$FRIDAY_DATABASE_PATH" \
  --files-root "$FRIDAY_HOME/data/files" \
  --tenant-id <tenant> \
  --owner-id <active-operator-owner> \
  --uploader-id <exact-uploader> \
  --report /path/private/report.json   # optional, mode 0600
```

Stdout JSON: counts, `plan_sha256`, short tags/classes only — no Telegram IDs,
paths, bodies, full content hashes, or full raw/user ids.

`plan_sha256` is computed from a **private** canonical basis (full raw id,
uploader, source_ref, content_hash, gates). That basis is never printed.

## Apply (explicit only)

Prerequisites:

1. Backend/bridge writers stopped (or guaranteed idle).
2. Verified backup directory (`--backup-dir`, mode 0700).
3. Claim manifest (mode 0600, not group/world-readable) with exact keys:
   `schema`, `claim_scope`, `approved=true`, `tenant_id`, `owner_id`,
   `uploader_id`, `candidate_count`, `plan_sha256`.
4. CLI: `--apply --claim-manifest … --expect-count N --expect-plan-sha256 … --backup-dir …`

Apply path: backup → BEGIN IMMEDIATE → replan CAS → modern-valid + disk recheck →
insert aliases → audit rows → integrity/FK → remaining plan must be 0. No force flag.

## Gates (plan + in-transaction CAS)

- Live `content_type=file`, `source=upload`, not deleted
- Not audio-document, privacy OK, exact `uploaded_by`
- **`NOT EXISTS inbox … status='ignored'`** (same as production binder/resolver)
- Modern-valid registration (`classify_file_registration` → `registered_valid`)
- Disk proof via shared `verify_registered_file_bytes` under `--files-root`
- Conflicting alias fail-closed
- Legacy / invalid / missing / hash-mismatch → zero candidate (closed counts only)

## Audit action

`cli.telegram_file_aliases.backfill` (allowlisted). Audit `after_json` uses tags only.
