# Recorded application workflow and operational boundaries

The supplied screen recording demonstrates a fast, user-operated sequence:
open a job, reuse a saved profile or previous application where offered, choose
an existing role-matched PDF, complete the visible required controls, submit,
and move between open tabs. The same session includes LinkedIn Easy Apply,
company-hosted forms, and multi-step applicant tracking systems. A visible
confirmation, rather than the final click, establishes the outcome. Switching
away while a page is still submitting leaves the outcome uncertain.

A recording proves only what it visibly shows. It does not independently verify
that an entered answer is true, what a truncated filename means, the bytes of a
previously uploaded CV, or the outcome of a page whose receipt is not visible.
Prefilled contact, address, salary, availability and work-authorization values
must be checked against current sourced evidence; entries from one employer are
not universal facts. Historical answers are never promoted automatically to the
verified profile. Read the actual posting and original CV for each application.
Role scope, workplace preference, employer exclusions and current authorization
come from the user and may be narrower than the roles in a historical recording.

The active executor should:

1. Check the live ledger, URL aliases and employer policy before opening a form.
   Preserve concurrent claims, submitted records and uncertain submissions.
2. Select an unchanged existing CV based on its contents and job requirements;
   verify the local file hash and the attachment shown by the site.
3. Reuse supported prefill only after comparing every visible value with its
   verified source and the actual field meaning. Skip unsupported required
   questions; leave optional unknowns empty. Do not infer facts from a recording.
4. For each visible non-secret field, record its label, before value, entered
   value, read-back, source and timestamp using `scripts.log_application_fields`.
   Stop the current application if a written field cannot be read back or if
   the read-back differs. The helper reports this but does not itself operate a
   browser or enforce a submission gate in every adapter.
5. Recheck employer and attachment, submit once when authorized, and record a
   positive job-specific confirmation. A click with no confirmation is uncertain
   and must not be retried automatically. Record blockers and continue.
6. Keep activity, screenshots, recordings, CVs and the database private; export
   and encrypt before publishing. Never include keys, credentials, codes, cookies,
   browser profiles, or session stores in a backup. Checkpoint frequently and
   verify the remote ciphertext and restoration before calling it backed up.

The LinkedIn v2 adapter now stops if a planned field cannot be filled and read
back exactly. This is a targeted guard, not proof that any platform is fully
qualified for unattended submission. The field logger is a local recording API;
it is not automatically connected to every browser adapter. CAPTCHA, required
legal agreements and account recovery must be skipped when their required
confirmation or handoff is unavailable. No long-running executor or automatic
repository-push service is installed by this document.

## Local model qualification status on 2026-11-21

The checked-in local-model report was generated with an earlier set of runtime
packages. Its source-integrity record does not match the current
`config/qualified_runtime_packages.json`, so the report is stale for the
current runtime. The currentness and aggregate-report tests failed in CI for
that reason. This mismatch predates the recording-workflow changes.

Before relying on the local-model qualification gate, rerun the real evaluator
with the exact model, server, Redis lease, Python and package versions required
by the current qualification configuration. Publish a new report only if the
real run succeeds; do not change report hashes to make an old run appear current.
Even a passing synthetic-fixture report does not qualify an employer site or
prove the correctness of answers on live forms.
