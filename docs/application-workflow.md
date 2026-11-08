# Reusable application workflow

The next phase builds on the existing active browser LLM and persistent SQLite
ledger. It converts recorded application experience into local knowledge and
turns a newly observed job/form into a work packet. The active LLM consumes the
packet, reviews the posting/CV and uses supported browser tools to apply.

The user asked to proceed automatically with more jobs and to remember the
procedure. This implementation reuses the existing Python workflow, rather than
introducing a second browser session or a scheduled unattended runner. No new
dependencies, external service or recurring schedule is needed.

## Refresh the knowledge

```powershell
python -m scripts.application_workflow refresh `
  --cv-dir '<existing role-specific CV directory>' `
  --verified-facts runtime-data/verified-answer-facts.json
```

Default outputs are `runtime-data/verified-answer-cache.json` and
`runtime-data/application-knowledge.json`. All applicant-specific outputs remain
under ignored `runtime-data/`. The source database is opened read-only.
Later refreshes preserve the reviewed manifest referenced by the existing cache
when `--verified-facts` is omitted; a missing/invalid source fails instead of
silently dropping learned answers.

The knowledge contains the workflow steps, status counts, an index of exact
verified facts, historical CV choices with file hashes, and observed step-type
patterns grouped by the source URL's provider. Examples point back to job and
receipt records. It does not copy raw form values, question answers, credentials,
cookies, recordings or screenshots into procedural recipes. A provider pattern
describes past activity and requires fresh inspection of the current form.

Facts come from the verified contact overlay, configured locality and separately
reviewed source manifests accepted by the answer cache. A submitted application,
job requirement or inherited prefill cannot establish a reusable fact. The
profile's attachment/text extractor and the older question-only LLM answer cache
are intentionally bypassed. See [verified-answer-cache.md](verified-answer-cache.md).

## Prepare the next observed job

After reading the actual live job and form, the LLM saves a private input like
this synthetic example:

```json
{
  "job": {
    "url": "https://employer.test/jobs/42",
    "title": "Python Developer",
    "company": "Example Employer",
    "description": "Observed posting text"
  },
  "fields": [
    {"field_id": "first", "label": "First name", "required": true},
    {"field_id": "email", "label": "Email", "required": true},
    {"field_id": "cv", "label": "Resume", "kind": "file", "required": true},
    {"field_id": "salary", "label": "Expected salary", "required": false}
  ]
}
```

```powershell
python -m scripts.application_workflow prepare `
  --input runtime-data/observed-next-application.json `
  --output runtime-data/next-application-packet.json
```

Exact ordinary contact labels are resolved from source-checked facts. Required
unknown fields block the job; optional unknowns are skipped. Offered demographic
decline choices and generic supplementary comments follow the existing no-input
policy. Required factual, legal or consent questions remain unsupported until
their actual meaning and applicable evidence are established.

To use a reviewed experience fact, the LLM supplies `fact_key`, `scope`, and
`meaning_reviewed: true` only after comparing the real question with the source.
The key/scope must exactly match the cache. These annotations belong to the LLM's
review, not metadata copied from an employer page. General tenure never becomes
a duration for every technology. An offered choice must match the supported
value; ranges are not guessed or coerced by this command.

The optional `cv_file` and `cv_reason` fields specify an existing file the LLM
has reviewed. Without them the packet uses an existing selection or a filename
suggestion. The actual posting and CV still need review. An exact Resume/CV file
field can produce an `upload_cv` instruction with a verified file identity;
other required attachments block until supported.

## Act on the packet

Each packet rereads the live ledger and employer policy. It checks canonical
and previously recorded source URLs, so a terminal job is skipped and an
uncertain/in-progress attempt requires reconciliation. Previously blocked jobs
require the existing explicit recovery path. Changed source files disable cached
answers until refresh. No packet claims a job or changes its recorded outcome.

For a new eligible job, continue through the existing planner and claim flow in
[browser-batch.md](browser-batch.md). Because `claim` takes the next prepared job
globally, compare its returned canonical URL with the intended job before any
browser action. Parallel lanes must also use their shared reservations/target
coordinator. A packet alone cannot reserve capacity.

Immediately before clicking Submit, run the live exclusion/CV/state check:

```powershell
python -m scripts.plan_browser_batch pre-submit-check `
  --url 'ORIGINAL_JOB_URL' `
  --observed-employer 'ACTUAL VISIBLE EMPLOYER' `
  --observed-url 'CURRENT_FORM_URL'
```

Use the actual employer label and observed redirected URL. See
[employer-policy.md](employer-policy.md). The persistent local policy carries the
user's employer exclusions and their provenance; historical CV employment and
filesystem directory names are not target employers.

Review required answers, exact attached filename and declarations, then submit
under current authorization. Save a positive receipt and record `submitted` only
after observation. A final click without confirmation is `submission_uncertain`
and must not be blindly retried. Record and continue past concrete blockers.

Every packet reports `submission_ready: false` and `requires_live_review: true`.
The active browser LLM performs that review without routine user questions. These
fields describe the offline packet boundary; they do not add a user approval
step. A packet never claims that a browser/background executor is running.

## Validation

Focused offline tests cover employer exclusions, exact scoped fact reuse, source
tampering, duplicate/uncertain outcomes, CV identities and private data boundaries.
Live application results continue to come from the authoritative ledger and saved
receipts, not from test counts or prepared packets.
