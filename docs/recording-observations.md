# Recording review import

The offline importer saves **reviewed observations** from a local screen
recording into the existing append-only `browser_batch_activity` table. It does
not parse or watch a video, apply for a job, upload a CV, make an LLM request,
create a verified applicant fact, or change a job's application status. A
`submit_clicked` observation is not proof of a successful application. Even a
`receipt_visible` entry only records what a reviewer says was visible: separately
inspect and reconcile an actual job-specific receipt through the existing ledger
workflow before recording a submitted outcome.

The planner, CV selection, claim, blocker recovery, skip reconsideration, work
packet and pre-submit checks treat reviewed `submit_clicked`, `receipt_visible`
and `prior_applied` events as an unresolved prior attempt. They do not reapply
until the outcome is reconciled; submitted/already-applied outcomes remain
terminal. This guard does not change the recorded job status, infer success or
automatically clear an observation. The batch planner reports held jobs in
`recording_reconciliation_holds` and continues preparing other jobs. A falsely associated observation requires a
separate audited investigation; do not delete or edit the append-only event.
There is currently no supported negative reconciliation or clearance command in
this importer, so a later failed/not-submitted finding does not lift the hold.
An event added after a job was claimed is rechecked before submit. Operators
must serialize shared-browser actions: the pre-submit check does not lock the
browser or eliminate a race after the check returns.

Matching includes stored canonical/source URLs and exact LinkedIn job IDs on
`linkedin.com` and its subdomains, including regional URLs stored with older
job keys. Other cross-platform aliases are compared only if a previously
observed mapping is present in the job's source URLs. Employer/title similarity
alone does not prove two listings represent the same requisition.

Keep the video, JSON review and database in private local storage, not in Git.
Reviewers should include only what was actually visible; job requirements,
prefilled answers, selected files and a button click do not prove experience,
file contents, employer identity or submission. Do not put passwords, codes,
session data or other credentials into the review or its artifacts. The
activity secret filter catches common patterns, not every possible secret.

Create a JSON review in `runtime-data/` using a SHA-256 hash of the original
recording. Set `recorded_at` to the recording start time with an explicit
timezone; offsets are seconds from that start, not wall-clock times. This time is
reviewer-supplied; the importer does not independently verify it or the video's
duration. Use stable unique observation IDs: an existing ID cannot be changed.
The example below is synthetic.

```json
{
  "reviewed": true,
  "recording_path": "C:/private/example.mp4",
  "recording_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
  "recorded_at": "2026-11-21T16:13:12+02:00",
  "observations": [
    {
      "id": "at-12s",
      "offset_seconds": 12.5,
      "job_url": "https://employer.example/jobs/42",
      "company": "Example",
      "title": "QA Engineer",
      "step": "cv_selected",
      "description": "A CV filename was visibly selected in the file chooser."
    }
  ]
}
```

Permitted step labels are `job_opened`, `form_filled`, `cv_selected`,
`cv_upload`, `submit_clicked`, `receipt_visible`, `prior_applied`, `blocker`,
and `other`. Use only a visible, attributable HTTP(S) job URL. The reviewer is
responsible for establishing the job/URL mapping; different websites may post
the same job. Do not invent URLs or copy an unrelated tab's URL.

```powershell
python -m scripts.import_recording_observations `
  runtime-data/reviewed-recording.json `
  --ledger runtime-data/browser-batch.sqlite3
```

The command verifies the source file against its hash, validates the entire
batch, and appends observation events in one transaction. Reimporting the same
review is idempotent. Changing an existing observation with the same recording
hash and ID fails instead of rewriting the audit trail; use a new ID for a
separately reviewed correction. Moving a recording also changes its evidence
path and requires a new review; the command does not silently rewrite prior
events. JSON validation errors are intentionally generic to avoid printing
private values.
