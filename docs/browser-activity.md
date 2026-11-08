# Local application activity audit

Use the existing browser-batch database to save each observed page, field entry,
CV upload, navigation, submit attempt, confirmation, and blocker. The audit is
local, append-only, and independent of the job outcome ledger. Recording a
`confirmation` activity does not mark a job submitted; record that outcome
through the separately validated batch ledger after observing the result.

```powershell
python -m scripts.log_browser_activity append runtime-data/step-events.json --ledger runtime-data/browser-batch.sqlite3
python -m scripts.log_browser_activity export --ledger runtime-data/browser-batch.sqlite3 --run-id example-run --output runtime-data/activity-export.json
```

Input format:

```json
{"run_id":"example-run","events":[
  {"event_id":"example-step-001",
   "job_url":"https://example.com/jobs/123",
   "company":"Example", "title":"Software Engineer",
   "event_type":"form_field",
   "timestamp":"2026-11-07T20:00:00+02:00",
   "observed":{"label":"First name","value":""},
    "entered":{"label":"First name","value":"Avery"},
   "artifact_paths":["C:/absolute/path/to/sanitized-page-snapshot.json"],
   "details":{"control":"textbox","step":1}}
]}
```

Event types are `observation`, `form_field`, `upload`, `navigation`,
`submit_attempt`, `confirmation`, and `blocker`. `observed` and `entered` can
contain strings, lists, or structured objects. Keep them separate to preserve
what was visible before the action and what the executor actually entered.
`timestamp` defaults to the current UTC time; supplied timestamps must contain
a timezone. An independent `recorded_at` preserves ingestion time. Each event
also stores the original job URL, canonical job URL/id, run id, and sequence.

For uploads supply `cv_path`; the logger reads that existing file and records
its absolute path, size, and SHA-256. Optional `cv_sha256` must match. Screenshots
and full accessibility snapshots are referenced through `artifact_paths`, not
embedded in SQLite. The logger does not open, inspect, sanitize, or hash those
artifacts. Capture all relevant steps in the browser runtime and append events
promptly; this CLI does not observe browser activity automatically.

Use stable `event_id` and `timestamp` values when retrying a file append. An
identical event retry is idempotent. Reusing an event id with different content
fails, and the entire batch rolls back. A batch also fails before writing any
events if a CV/hash or prohibited-secret check fails. Database triggers reject
updates and deletion of audit rows through ordinary SQL. This is a local
integrity control, not a tamper-proof audit system against a database owner.

Personal profile and entered application information may be captured locally,
as authorized. **Never save passwords, verification/OTP codes, cookies, session
tokens, authorization headers, or recovery answers**, including in screenshots,
accessibility snapshots, raw input JSON, filenames, and URL query strings. The
logger rejects common credential keys/field labels, inline credential values,
and authentication parameters in URLs. It cannot reliably detect a secret
hidden in arbitrary prose or image contents. Sanitize source artifacts before
saving, redact credential values at capture, and log only the fact that a
sign-in/verification step occurred. Keep inputs/artifacts/exports under ignored
`runtime-data/` paths and never commit them.

Python callers can use `ActivityStore(path).append_batch(payload)` and
`ActivityStore(path).export(run_id=..., job_url=...)`. No network or browser
actions occur in this API. A confirmation route or submit click is only an
observation; a positive, job-matching result remains necessary for submission.
