# Employer exclusions

The local browser application workflow reads `employer-policy.json` beside its
ledger. For the default ledger this is ignored `runtime-data/employer-policy.json`.
Use `--employer-policy PATH` to select another policy. Keep personal employer
preferences in that local file, rather than in application source code.

```json
{
  "schema_version": 1,
  "exclusions": [
    {
      "id": "example-employer",
      "employer": "Example Employer",
      "aliases": ["Example Employer, Inc."],
      "domains": ["employer.example"],
      "reason": "Do not apply to this employer.",
      "source": {
        "type": "user_instruction",
        "text": "User explicitly excluded this employer.",
        "recorded_at": "2026-11-08T00:00:00+02:00"
      }
    }
  ]
}
```

Names match complete employer labels after case, whitespace and punctuation
normalization. Configured domains match that hostname and its subdomains, with
hostname boundaries. Known shared ATS hosts also check their employer tenant
segment against the configured employer aliases. Arbitrary job description
text, CV employment history, filenames, filesystem paths, URL query strings,
and arbitrary URL path components never establish the target employer.

Policy checks run again on import, preparation, CV selection and claim. Excluded
jobs become blocked and claims continue to the next eligible job. Resume and
skip reconsideration cannot bypass the policy. A policy absent from a workspace
preserves existing behavior. An existing malformed or unreadable policy denies
work until it is repaired; it does not silently discard the user's exclusions.

The external browser runner must run this gate immediately before each Submit
action, supplying the currently observed employer or current application URL:

```powershell
python -m scripts.plan_browser_batch pre-submit-check --url 'ORIGINAL_JOB_URL' --observed-employer 'VISIBLE_EMPLOYER' --observed-url 'CURRENT_PAGE_URL'
```

A nonzero exit means do not submit. The gate rereads current policy and checks
the stored employer, all recorded source URLs, and the fresh observed identity.
It also requires an in-progress job and an unchanged selected CV. This command
does not drive a browser or grant submission authorization. Existing user
authorization and the browser workflow's other checks still apply.

For research and reporting, `check-employer` accepts the same identity inputs
without requiring an in-progress state. Python callers can use
`evaluate_employer(employer, urls, policy_path=...)`,
`require_employer_allowed(...)`, `BatchLedger.check_employer(...)`, or
`BatchLedger.pre_submit_check(...)`. Policies are read freshly on every check.

Historical submitted, already-applied and uncertain records are preserved.
Recording an observed outcome is an audit operation, not permission to apply;
the policy does not withdraw applications or reinterpret recorded success.
