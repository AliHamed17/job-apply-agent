# Cached discovery from saved job URLs

`python -m scripts.discover_job_urls` creates a review manifest without opening a
browser, reading a profile/CV, writing to the application database, or submitting
an application. It complements the existing discovery worker; it does not enqueue
jobs or alter the worker's submission settings.

```powershell
python -m scripts.discover_job_urls --input runtime-data/saved-whatsapp-leads.json
```

Inputs can be WhatsApp text exports, a JSON list of URLs/lead objects, or JSON with
`leads`, `tabs`, `jobs`, `messages`, `urls`, or `events` containers. URL fields and
message text are scanned; raw message bodies, contact details, and unrelated JSON
fields are not copied into the output. Repeat `--input` and `--url` to combine
sources. Files are read as UTF-8, including UTF-8 with a BOM.

Public Greenhouse, Lever, Ashby, and Comeet HTML pages are eligible for a GET.
The existing JSON-LD and provider parsers extract posting details or listing
links. A listing entry without a description has `detail_required: true`; its job
description still needs review before CV selection or application. Dynamic pages
that cannot be parsed become browser handoffs. Public JSON board APIs are not
implemented by this command.

Company sites require an exact hostname opt-in. This avoids automatically
requesting arbitrary links copied from messages. Opt-ins do not override the
LinkedIn/Indeed browser policy, unsafe-URL checks, or private-address checks.

```powershell
python -m scripts.discover_job_urls --url https://careers.example.org/jobs/42 --allow-host careers.example.org
```

LinkedIn and Indeed URLs are canonicalized and queued as `browser_handoff` with
zero HTTP requests. An authenticated, supported browser workflow can review those
URLs later. HTTP 401/403/429, challenge pages, login routes, unsafe/unapproved
redirect destinations, oversized pages, and missing job details also produce
explicit outcomes. There is no headless fallback, account login, CAPTCHA solving,
form fill, submission endpoint access, or retry loop.

## Speed and saved flow

* Canonicalize provider URLs and strip tracking before fetching; duplicate input
  URLs share one result. This is URL identity only, not proof of an earlier
  application or a replacement for the authoritative application ledger.
* Fetch up to four pages concurrently, with at most two requests per host.
  `--concurrency` and `--per-host` adjust these bounds; the maximum is 16.
* Cache parsed public results for six hours by default. A repeated run can use
  cached results without network or parsing work. `--cache-ttl 0` forces
  revalidation; stale pages use ETag/Last-Modified where available. HTTP 304 or an
  unchanged body hash reuses the parsed result.
* Blocked/unavailable public pages have at most a five-minute cache lifetime.
  Network errors are not cached. One network failure does not abort other URLs.
* Preserve per-result GET, parser, redirect/handoff, and cache actions in `steps`.
  Statuses distinguish `discovered`, `browser_handoff`, `unavailable`, `rejected`,
  and `error`; none means applied. Cache timestamps and manifest `generated_at`
  are Unix seconds. The CLI prints a compact count summary.

By default, the cache is `runtime-data/discovery/cache.json` and the latest review
manifest is `runtime-data/discovery/latest.json`. Both are beneath the ignored
runtime directory. `--cache` and `--output` may choose separate local paths, but
must not overwrite input files. Use separate cache/output paths for separate
processes: the file cache is single-writer, while requests within one batch are
concurrent. Replacements are atomic and completed cache entries are saved on a
normal completion or cancellation. This is a response cache, not a durable
application journal.

Keep caches and manifests private and out of Git: posting descriptions and URLs
are public data, but saved-message inputs may contain personal material. Secret
query parameters and credential-bearing URLs are rejected without retaining the
raw URL. Literal/private hosts, non-HTTPS URLs, nonstandard ports, and hosts whose
DNS answers include nonpublic addresses are rejected. Redirects are checked at
every hop and never followed automatically by the HTTP client. The command only
requests explicit input pages and supported redirects; it does not recursively
crawl links. Split more than 500 input URLs into batches.

Before applying, the existing application planner must still review the actual
job details, deduplicate against canonical/provider identities in the ledger,
verify the selected original CV, and capture the actual submission receipt. No
discovery output authorizes or claims an application.
