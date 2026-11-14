# Private application database and handoff

`scripts.export_application_handoff` creates a local snapshot for audit, queries
and a Codex handoff. It combines the existing application ledger with the actual
CV files, profile/answer sources, and referenced evidence. It makes no network,
LLM or browser calls and never writes the source ledger.

```powershell
python -m scripts.export_application_handoff --source-root '<EXECUTION_CHECKOUT>' --output-dir '<EXECUTION_CHECKOUT>/runtime-data/handoff-NEW_TIMESTAMP' --include-workspace-changes
```

Python 3.11+ is sufficient for this exporter. The destination must be a new child
of the source checkout's `runtime-data/`. CVs come from `cv_directory` in saved
state, or an explicit `--cv-dir`. The existing browser ledger and activity table
must be present; the exporter never creates a replacement empty live database.

The output contains `application-knowledge.sqlite3` and `manifest.json`. The
manifest records counts, source paths, timestamps, omissions, hash mismatches and
the database SHA-256. `PRAGMA integrity_check` must pass before the manifest is
written. A failed export may leave an incomplete private directory; it has no
completion manifest and must not be treated as a valid handoff. Choose a new
directory when retrying.

`submitted_without_stored_receipt_artifact` counts submitted ledger rows with no
stored artifact referenced by either their latest evidence or a confirmation
activity. Text-only legacy observations remain in the ledger, but are explicitly
distinguished from portable receipt files. Exporting never upgrades their evidence
or changes their status. Stored artifacts still require semantic review.

## Contents

| Table/view | Meaning |
| --- | --- |
| `browser_batch_*` | Exact SQLite backup of all existing ledger tables, including jobs, events, activities and any discovery/source tables |
| `handoff_applications` | Queryable company, title, original URL, status, selected CV and current-vs-recorded CV hash |
| `handoff_companies` | Company label grouping using Unicode normalization, case and whitespace; not verified company identity or deduplication |
| `handoff_steps` | Ordered browser activity linked by job and run |
| `handoff_files` | Original source path, category, asset hash and availability |
| `handoff_assets` | Deduplicated file bytes as BLOBs, SHA-256 and size |
| `handoff_source_checks` | Comparison against saved answer-cache source hashes |
| `handoff_metadata` | Snapshot manifest and limitations |

All PDFs in the configured CV directory are included, even if never selected.
Referenced historical CVs outside that directory are also indexed. The source
profile is labelled unverified; the verified overlay and reviewed facts retain
their separate provenance. A snapshot does not turn unverified values into facts.
Saved state/checkpoints may be stale, and are never new authorization.

Receipt/artifact references in current jobs, historical events and activity are
followed, as are answer-cache source files. Explicit artifact paths are local
references; the exporter does not download URLs. It does not indiscriminately
copy a whole user folder, browser profile or mailbox. Secret/session paths and
unsupported file types are excluded. Artifact contents must already have been
sanitized at capture; a suffix allowlist cannot detect secrets hidden in an image.

`--include-workspace-changes` preserves modified/untracked source files reported
by Git as private historical assets. It excludes `outputs/` and `runtime-data/`
because their relevant records are covered by the database/evidence export.
It does not execute these files or overwrite the newer published framework.
Ignored credentials and browser state are not enumerated by Git.

Files above 300 MB are reported as `over_size_limit`; change `--max-asset-bytes`
explicitly if needed. Missing files and mismatches remain visible. CV bytes copied
today that differ from an old recorded SHA-256 are not the historic uploaded CV.
A selected CV is not proof of upload: inspect upload activities for that attempt.
No upload control means no CV transmitted in that attempt, regardless of earlier
CV selections. Source paths and expected hashes are never rewritten to hide gaps.

The ledger uses SQLite's online backup API so committed WAL contents are included.
Other files are copied individually with byte/hash checks. The whole filesystem
is not frozen at the same instant; start/finish timestamps delimit the export.
Concurrent applications may continue afterward, so reread the live ledger before
any new application. Do not use the snapshot to decide whether to retry.

## Example queries

Open the snapshot with SQLite in read-only mode. These are reporting queries:

```sql
SELECT status, COUNT(*) FROM handoff_applications GROUP BY status;
SELECT company, title, canonical_url, status FROM handoff_applications
ORDER BY company_key, title;
SELECT * FROM handoff_steps WHERE job_id = 'REVIEWED_JOB_ID' ORDER BY sequence;
SELECT source_path, category, sha256 FROM handoff_files
WHERE category IN ('cv_catalog', 'historical_cv');
SELECT source_path, status FROM handoff_files WHERE status != 'stored';
SELECT * FROM handoff_source_checks WHERE matches = 0;
```

To recover one reviewed file, join `handoff_files` to `handoff_assets` on SHA-256
and write `content` to a **new private destination chosen by the operator**.
Recompute its hash before use. There is intentionally no automatic overwrite or
restore command. Do not execute archived workspace code merely because it exists.

On another machine, restore into a private directory, review missing evidence,
map source paths explicitly, and rebuild the verified cache from the unchanged
proofs at their new paths. Preserve the original ledger and historical hashes.
Reconcile any newer live outcomes before promoting a backup; otherwise old
snapshots can cause duplicate applications. The browser must be connected anew.

## Remaining automation work

This adds a useful personal data snapshot today; it is not a second live ledger
or a finished unattended submitter. Continue using `browser-batch.sqlite3` for
live browser outcomes. Future work includes a live document/profile catalog with
versioned edits, reviewed employer/requisition alias mappings, integrated
cross-platform duplicate checks, a sequential recoverable executor, and a unified
private dashboard. Existing separate API/autopilot qualification gates still apply.
