# Codex job-application handoff

Read this file first when continuing an **active Codex browser application run**.
The CLI prepares work and records evidence. The LLM operates the connected browser
under the user's current authorization. No background application service is
started by this handoff.

## Establish the current state

1. Identify the actual execution checkout. A release worktree and the laptop's
   active application checkout may differ. Do not replace an active checkout or
   change its branch while another session is using it.
2. Read the current user request, `runtime-data/browser-batch-state.json`, the
   latest run checkpoint, and `runtime-data/employer-policy.json`. State files can
   lag the database. A historical count or target is not the current run's result.
3. Confirm that `runtime-data/browser-batch.sqlite3` exists **before invoking the
   planner**, whose constructor can create an empty ledger. This database is the
   authority for this browser workflow. Do not substitute `job_agent.db`, the
   separate API/PostgreSQL workflow, a CSV, or a handoff snapshot.
4. Read `user_profile.yaml`, the verified overlay named by state, the reviewed
   facts manifest, and the source-bound answer cache. A placeholder profile,
   inherited prefill, job requirement, or successful application proves no fact.
   Validate source hashes before reusing answers.
5. Resolve the existing CV directory from state. Read the actual posting and CV.
   A matching filename or historical selection is only a suggestion. Keep all
   original CVs unchanged. Preserve conflicting contact details with provenance.
6. Inspect existing claims, uncertain outcomes, and active sessions before doing
   browser work. Use one executor per job. A saved packet is not a reservation.

Read [browser-batch.md](docs/browser-batch.md),
[application-workflow.md](docs/application-workflow.md),
[browser-activity.md](docs/browser-activity.md),
[verified-answer-cache.md](docs/verified-answer-cache.md), and
[employer-policy.md](docs/employer-policy.md) for the exact schemas.

## Resume and apply

Use the user's authorized roles, employers, batch size and time window. Saved
instructions do not authorize additional submissions. Within an authorized batch,
continue independently and record concrete blockers without routine questions.

1. Inventory only the relevant current browser tabs using supported browser/UI
   tools. Preserve signed-in sessions and unrelated tabs. Read the tool's current
   instructions; do not reuse old tab IDs, coordinates, browser internals, or
   hidden session data. Reobserve after navigation and form transitions.
2. Before opening an application form, check the original/canonical URL, every
   known source URL alias, and normalized employer against the live ledger and
   exclusions. Compare employer + title across platforms as a **probable** duplicate.
   Different URLs alone do not prove different jobs; equal titles alone do not
   prove identical requisitions. Shared thank-you URLs are not job identities.
3. Never reapply to a receipt-backed submitted or already-applied record. Inspect
   `in_progress` and `submission_uncertain` outcomes before further actions. If
   Submit may have happened, reconcile rather than retry. The separate proposed
   cross-platform guard is not assumed to be integrated into this CLI.
4. Import observed job metadata, select the reviewed CV with a reason, then claim
   a prepared job. **`claim` takes the next eligible job globally, not a URL.** Act
   only for its returned `in_progress` record. An empty/blocked claim means stop
   that action; recheck the posting/CV if the claimed URL differs.
5. Verify the CV hash before upload. If uploads require a workspace-local file,
   stage an unchanged copy under `runtime-data/upload-staging/`, compare hashes,
   and inspect the displayed attached filename. An upload attempt is not proof
   that the file attached. If the form has no upload control, record **no CV
   transmitted** and keep any earlier application's CV provenance separate.
6. Fill only source-backed answers. Exact key, scope, and question meaning must
   agree. General employment tenure is not experience with every technology;
   studying for a degree does not establish completion. Leave optional unknowns
   empty. Unsupported required factual/legal answers block that job. Do not use
   punctuation, invented qualifications, arbitrary zeroes, or default Yes answers.
7. Use ordinary authorized sign-in. If authorized for that run, retrieve only a
   recent matching site/account verification message through a mail connector.
   Enter the code only into the expected form; never save it. CAPTCHA, account
   recovery, unavailable credentials, or unsupported required confirmations remain
   concrete blockers. Record them and continue to other eligible jobs.
8. Immediately before Submit, rerun the live employer/CV/state gate using the
   observed employer and current form URL. Review required fields, the exact
   attachment and declarations, then submit once under current authorization.
9. Observe a positive receipt tied to that application. Save sanitized evidence,
   append the activity and separately record the outcome. A click, redirect,
   HTTP success, or generic contact-form message is not enough. Unrecognized
   receipts require review; never weaken a matcher just to increase the count.
10. Close only completed task tabs when appropriate, record closure as an
    observation, export the ledger, and continue until the authorized scope has
    observed outcomes or concrete blockers. On interruption, save the pending URL,
    last action, last observed state and next step without resetting claims.

## Commands

Run from the execution checkout. Substitute observed values; these examples do
not supply applicant facts or authorize submissions.

```powershell
python -m scripts.plan_browser_batch list --output runtime-data/current-results.json
python -m scripts.plan_browser_batch catalog --cv-dir '<CV_DIRECTORY>'
python -m scripts.application_workflow refresh --cv-dir '<CV_DIRECTORY>' --verified-facts runtime-data/verified-answer-facts.json
python -m scripts.prepare_application_batch --input runtime-data/observed-applications.json --output-dir runtime-data/prepared-runs/NEW_RUN
python -m scripts.plan_browser_batch plan runtime-data/observed-tabs.json --cv-dir '<CV_DIRECTORY>'
python -m scripts.plan_browser_batch select-cv --url '<JOB_URL>' --cv-dir '<CV_DIRECTORY>' --file '<EXISTING_CV.pdf>' --reason '<REVIEWED_MATCH>'
python -m scripts.plan_browser_batch claim
python -m scripts.plan_browser_batch verify-cv --url '<CLAIMED_JOB_URL>'
python -m scripts.plan_browser_batch pre-submit-check --url '<CLAIMED_JOB_URL>' --observed-employer '<VISIBLE_EMPLOYER>' --observed-url '<CURRENT_FORM_URL>'
python -m scripts.log_browser_activity append runtime-data/step-events.json
python -m scripts.plan_browser_batch record --url '<CLAIMED_JOB_URL>' --status submitted --evidence-json runtime-data/observed-receipt.json
```

Only the last command records success, and only after actual confirmation. For a
click without confirmation, use `submission_uncertain` with `submit_clicked`
evidence. `blocked`, `closed`, `skipped`, and `already_applied` require their own
observed reason. Keep original job URLs when redirects lead to shared receipts.
Explicit `resume-blocked` and `reconsider-skipped` have evidence requirements;
neither is a way to retry an uncertain/submitted job.

## What to save and report

Record the sequence: job review -> factual answers -> exact CV upload or no
upload -> one submit attempt -> receipt/blocker -> tab closure. Use stable event
IDs, timestamps, run IDs, source URLs, selected file/hash, upload file/hash and
artifact pointers. The activity log and outcome ledger serve different purposes.
Do not store passwords, cookies, security codes, session tokens or raw auth fields.

Report **new confirmations in this run**, earlier confirmations, prior
applications, uncertainties, blockers, closed/skipped jobs and prepared work
separately. Count distinct employers only after reviewing employer identities;
application totals and company totals are not interchangeable. Tests, packets and
historical receipts are not new applications.

## Private database and portability

[private-application-handoff.md](docs/private-application-handoff.md) documents an
offline export containing the ledger, source documents, CV bytes and evidence in
one queryable SQLite snapshot. Keep it local. A GitHub clone contains the software
and templates, not the applicant's private database, CVs or active browser session.

The active browser workflow and the separately qualified API/autopilot system are
different execution paths. Do not enable global submission flags, schedules or
autopilot to resume a browser session. The latter has its own qualification gates.
Read [low-cost-automation-plan.md](docs/low-cost-automation-plan.md) for implemented
preparation and the remaining unattended-executor work.
