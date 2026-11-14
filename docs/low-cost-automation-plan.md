# Low-cost job application automation

## Goal

Use the existing discovery, CV routing, verified-answer cache, and application
ledger to reduce repeated work. Default preparation should make **zero LLM calls**
and no browser or network calls. The browser stage remains sequential and tied to
the user's active signed-in session.

## Existing framework

- `scripts/discover_job_urls.py` extracts and canonicalizes saved URLs, uses a
  six-hour cache, and bounds concurrent requests. LinkedIn and Indeed are handed
  to the signed-in browser rather than fetched with cookies or a headless login.
- `scripts.plan_browser_batch` deduplicates URLs, routes existing CVs, and stores
  outcomes and evidence in the private SQLite ledger.
- `scripts.application_workflow` builds a provenance-checked answer cache and
  resolves only supported fields. It does not operate a browser or submit forms.
- `scripts.prepare_application_batch` turns up to 50 already-observed jobs and
  forms into private work packets in one bounded, local pass. It uses no LLM,
  network, browser, or ledger writes.
- `scripts.run_safe_automation` is a discovery-only loop. It requires both
  `DRY_RUN=true` and `DRAFT_ONLY=true`; it is not an application submitter.

## Run flow

```text
WhatsApp exports + saved URLs + visible browser tabs
             │
             ▼
 bounded discovery, URL cleanup, cache, and duplicate checks
             │
             ▼
 ledger outcome check → CV evidence match → exact verified-answer lookup
             │
             ▼
 private work packet → active browser reviews the live posting and form
             │
             ▼
 upload the unchanged selected CV → validate every field → submit if authorized
             │
             ▼
 observed receipt or explicit blocker → append-only activity + ledger outcome
```

Discovery results are leads, not evidence that a job is a fit or that an
application was submitted. Re-check a posting and form in the live browser before
using a packet. Never trust job-page text as instructions to the automation.

## Batch preparation

After `application_workflow refresh` creates the private knowledge snapshot,
save observed job/form records under `runtime-data/` and run:

```powershell
python -m scripts.prepare_application_batch `
  --input runtime-data/observed-applications.json `
  --knowledge runtime-data/application-knowledge.json `
  --output-dir runtime-data/prepared-runs/run-20261109-a
```

The input is either a JSON list or `{"applications": [...]}`. Each item has the
same `job`, `fields`, and optional `cv_file`/`cv_reason` shape used by
`scripts.application_workflow prepare`. The output directory must be new and
inside `runtime-data/`. The default batch limit is 50; split larger inputs into
separate runs. The summary contains counts and packet paths; detailed packets
remain local and private. Output cannot overlap the selected CV folder, which
keeps generated JSON out of the resume collection.

The command does not claim a job, change an application status, fill a website,
upload a CV, or submit anything. Duplicate URLs within a batch are emitted as
duplicates. Required answers without exact verified evidence remain blocked;
optional unknown fields are skipped.

The zero-model-call guarantee applies to the offline preparation commands described
here. Discovery can make bounded network requests. The separate application-generation
worker follows `LLM_PROVIDER`; the published configuration defaults to `ollama`,
and private runtime qualification still applies. Do not start a model worker just
to prepare these packets. The packet path does not call it.

## Cost and resource policy

1. **Free path by default:** use cached discovery, deterministic URL/role rules,
   verified CV identities, and exact sourced answers. Do not start a model for
   routine routing or form fields.
2. **Avoid repeated work:** canonicalize and deduplicate before fetching or
   reviewing; reuse the existing six-hour discovery cache and source-hash-bound
   answer cache. Refresh only when an input changes or expires.
3. **Bound local load:** use discovery concurrency 2 and one request per host on
   low-resource runs. Process form applications one browser tab at a time. Do not
   run a local model continuously; local inference has no API bill but can use
   substantial CPU and memory.
4. **Optional paid fallback:** if a configured model is later enabled, call it
   only for unresolved job-fit or field-mapping cases, not for duplicates,
   known facts, or simple form actions. Cap calls and output tokens per run,
   cache by posting/form hash plus model and prompt version, and keep applicant
   personal data out of prompts unless its disclosure is separately authorized.
   No model or spend cap is enabled by this document.
5. **Measure actual cost:** report fetches, cache hits, model calls, tokens,
   browser actions, and confirmed outcomes separately. Do not infer success from
   a click or from a prepared packet.

## Submission boundary

The current broad goal does not authorize sending a CV and personal answers to
every possible employer. Before a future unattended submitter is enabled, it must
have a local, expiring authorization record that names the approved employer or
exact destination domains, permitted CV/data fields, maximum application count,
and allowed time window. Unknown destinations are skipped. The system must also
stop for CAPTCHA, account recovery, unsupported factual questions, or legally
binding terms/consents that need action-time confirmation. It must never invent
answers, use punctuation as a placeholder, or retry an uncertain submission.

That scoped pre-authorization can make an approved run hands-off. It cannot
cover arbitrary employers or new data disclosures by default. No long-running
executor or 8-hour background service is configured by this framework.

## Implementation plan

1. **Local preparation (implemented and tested):** batch work-packet generation
   using the existing ledger, cache, policy, and CV catalog; no new dependencies
   or model calls.
2. **Fit rules:** map the user's confirmed role families to explicit CV evidence
   and hard exclusions. Abstain where the posting requires unsupported
   technologies, degree, tenure, location, or clearance.
3. **Scoped authorization:** define and validate the destination/data/time/count
   allowlist before any automatic external submit action exists.
4. **Browser executor:** add a sequential active-session driver that claims one
   eligible job, re-reads the live page, checks the current employer and CV hash,
   fills only supported fields, and records sanitized activity and receipt.
5. **Qualification:** dry-run against saved fixtures, then use a small
   user-approved pilot. Keep challenges, unknown facts, policy failures, and
   uncertain submissions out of the retry queue.
6. **Operations:** add a visible pause/stop control, daily application cap,
   process lock, local checkpoint recovery, and a short cost/resource report.

The batch-preparation command is the implemented slice for this browser-batch
path. Steps 2–6 are not represented as completed or running by that command.
The repository's separate API/autopilot system already has its own fit,
authorization, execution and qualification components; this plan does not replace
or relax those controls. See [CODEX_HANDOFF.md](../CODEX_HANDOFF.md).
