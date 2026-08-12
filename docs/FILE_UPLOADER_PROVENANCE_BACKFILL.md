# Historical file-uploader provenance

`tools/backfill_file_uploader_provenance.py` is an offline, fail-closed
maintenance tool for legacy CLI imports whose `uploaded_by` field was absent.
It does not infer authorship from the shared tenant, filesystem ownership, a
filename, or the current administrator. Legacy Telegram mappings are audited
and counted, but this tool never changes them.

Run preview first while Friday is stopped:

```bash
.venv/bin/python tools/backfill_file_uploader_provenance.py \
  --database /absolute/path/to/jericho.sqlite3 \
  --tenant-id TENANT_ID \
  --owner-id OWNER_ID \
  --report /private/path/uploader-preview.json
```

The report is created atomically with mode `0600` and contains no filenames,
paths, Telegram IDs, bodies, or file digests. Review `candidate_count` and
`plan_sha256`. If the named owner explicitly claims the whole bounded scope,
create a private claim manifest:

```json
{
  "approved": true,
  "candidate_count": 1671,
  "claim_scope": "all_unattributed_cli_imports",
  "owner_id": "OWNER_ID",
  "plan_sha256": "COPY_THE_64_HEX_PREVIEW_VALUE",
  "schema": "friday.file-uploader-owner-claim.v1",
  "tenant_id": "TENANT_ID"
}
```

Apply only after an independently verified database backup location is ready:

```bash
.venv/bin/python tools/backfill_file_uploader_provenance.py \
  --database /absolute/path/to/jericho.sqlite3 \
  --tenant-id TENANT_ID \
  --owner-id OWNER_ID \
  --apply \
  --claim-manifest /private/path/owner-claim.json \
  --expect-count 1671 \
  --expect-plan-sha256 COPY_THE_64_HEX_PREVIEW_VALUE \
  --backup-dir /private/path/backups \
  --report /private/path/uploader-applied.json
```

There is no force mode. Apply takes `BEGIN IMMEDIATE`, recomputes the plan,
requires the manifest, count, and checksum to agree, creates and verifies an
online pre-change backup, and performs compare-and-swap updates. Every changed
Raw Object gets an append-only `cli.file_uploader.backfill` audit row in the
same transaction. Integrity and foreign keys are checked before commit and
again afterward.

Eligible rows must be live file Raw Objects in the exact tenant with bounded,
duplicate-free object metadata, one non-empty `import_source_path`, and no
`uploaded_by` key at all. Explicit `null`, an existing uploader, malformed or
oversized metadata, and every non-import row are refused. Only `uploaded_by` is
added; source identity, content hashes, file bytes, Inbox and Knowledge Objects
are untouched.
