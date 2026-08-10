# P1 (revised) — Lever-first capture and selector rebuild

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Supersedes** the P1 target in
[`2026-08-03-earned-autonomy-auto-apply-design.md`](../specs/2026-08-03-earned-autonomy-auto-apply-design.md)
§7, which names Greenhouse as the first target. That choice is no longer correct;
this document explains why and replaces it with Lever, using only evidence
gathered since — no part of the corrected priority is assumed.

**Goal:** one `Submission` with `outcome == "confirmed_submitted"`, backed by a
real `SubmissionEvidence` row and a `LIVE_CANARY_QUALIFIED` adapter record — the
same P1 exit criterion the design spec already defines, now aimed at the adapter
that can actually reach it.

**Architecture:** two operator-gated evidence captures (already tooled, zero new
code required to run them) feeding one selector-contract rewrite, which is the
only step this plan can't pre-write, because writing it now would mean
guessing — precisely what P0's Task 6 and the transport probe both exist to rule
out.

---

## Why Greenhouse was retargeted to Lever

Verified this session, reproducible without a browser:

```bash
for slug in gitlab stripe airbnb doordash pinterest; do
  curl -s -o /dev/null -w "$slug -> %{http_code} %{redirect_url}\n" \
    "https://boards.greenhouse.io/$slug"
done
```

All return `301` to `job-boards.greenhouse.io` — a platform-wide sunset, not a
per-tenant migration. The new domain's raw (pre-JS) HTML already shows
`<form method="get" ...>` and zero `data-field-id` occurrences — `curl`, no
Playwright, no application spent. `submitters/greenhouse_v1.py`'s
`_FIELD_WRAPPER_SELECTOR` and `submitters/greenhouse_playwright.py`'s
`structureReady()` both hard-require the opposite (`method=post`,
`enctype=multipart`, `data-field-id`). This is not a selector typo; the v1
adapter's entire premise doesn't hold against any real posting today. Fixing it
means a new transport (§6.1 of the design spec already flagged the field
extraction half of this; the transport half — GET vs POST — was found this
session and is strictly worse).

Lever, by contrast:

```bash
curl -sL "https://jobs.lever.co/gopuff/<posting>/apply" | grep -oE '<form[^>]*>'
# <form id="application-form" enctype="multipart/form-data" method="POST">
```

Confirmed on two independent tenants (gopuff, shieldai) via the
`ats_transport_probe.py` research (merged, PR #57) and cross-checked again via
plain `curl` this session. The transport `structureReady()` needs — native
POST, multipart — is what Lever actually serves. **The defect is narrower**:
`LEVER_FORM_SELECTOR` requires `data-qa="application-form"][data-posting-id]
[data-site]` on the `<form>` element; real markup has none of those three, only
`id="application-form"`. Fixable as a selector-contract update, not a
transport rewrite.

---

## What's already true — no further action needed

- [x] **Real operator profile.** `profile_version >= 1` against the containerized
  Postgres stack, zero `PROFILE_*` reason codes. Real name, contact, citizenship,
  work authorization (IL confirmed / US requires sponsorship / EU abstains —
  correctly, since it was never confirmed), target roles.
- [x] **`cv_routing.yaml`** — 16 real role-specific CVs (from the operator's own
  company × role CV set), routing config validates against `profile/cv_routing.py`'s
  schema.
- [x] **Infra.** Postgres, Redis, `celery-worker`, `celery-beat`, `web-api` — all
  up via `docker compose`, migrations applied, healthy.
- [x] **Both capture tools built and tested**, mirroring each other's safety
  design (never fills, never uploads, never submits; aborts before Step 2 if
  the blank form already fails a tripwire so a bad target can't cost a real
  application to learn):
  - `scripts/greenhouse_selector_capture.py` — kept for completeness; **do not
    run it yet**. The transport finding above means it will trip
    `FORM_METHOD_NOT_POST`/`FORM_ENCTYPE_NOT_MULTIPART` on the blank-form check
    and abort in seconds. Real value here is after a future Greenhouse
    transport rewrite, not before.
  - `scripts/lever_selector_capture.py` — the one to run. Confirmed via `curl`
    that its `_FORM_CANDIDATES`/tripwire logic matches real Lever markup shape;
    the transport-level tripwires should **not** fire on a normal posting.

## Greenhouse: what a future transport rewrite would need (not scheduled — scoping only)

Same read-only method used on Lever below, run against a live, junior-level
Greenhouse posting (`job-boards.greenhouse.io/waymark/jobs/4711827005`) to
de-risk *future* work, not to start it now. Confirms and sharpens §6.1's
finding:

- **Proof, not suspicion, that submission can't be a native form POST.**
  `method="get"` on a form with two `<input type="file">` children is not
  just wrong per `structureReady()` — it's structurally incapable of carrying
  file content at all (GET requests have no body). The real submit is
  necessarily a JS-driven API call. `SUBMIT_IS_XHR` was always the right
  tripwire name for this ATS.
- **Every field is identified by `id`, not `name`** — `first_name`,
  `last_name`, `preferred_name`, `email`, `phone`, `candidate-location`,
  `resume`, `cover_letter`, `school--0`, `degree--0`, and per-posting custom
  questions as `question_<numeric-id>` (a Greenhouse internal question ID,
  not a UUID like Lever's cards). Every `name` attribute is empty. Any future
  rewrite keys off `id`, full stop — there is nothing to read from `name`.
- **The real wrapper class is `.field-wrapper`**, not `data-field-id`
  (confirming §6.1) and not `data-testid` either, though `data-testid`
  appears elsewhere in the form and may be worth a second look when this work
  is actually scheduled.
- Two file inputs confirmed present on a real posting (resume + cover
  letter) — the existing `MULTIPLE_FILE_INPUTS` tripwire would correctly
  flag this exact posting as needing the single-file-first-proof rule applied
  carefully, or a different target chosen.

This does not change P1's priority. Lever remains the near-term target
because its transport already matches what the codebase assumes; Greenhouse
needs the transport rewrite itself before a capture is even worth running.
Recorded here so that whenever that work is scheduled, it starts from real
field-identity evidence instead of the assumptions that produced the original
22 fixtures.

## Read-only reconnaissance done since (2026-08-05, live browser, blank form only)

Loaded `https://jobs.lever.co/palantir/c4442730-2926-41ad-8c0e-5e5a6b4d14ae/apply`
in a real browser and read the rendered DOM — no field typed, no file chosen, no
button clicked. This is exactly Step 1 of `lever_selector_capture.py`, run by
hand once to sanity-check the tool's assumptions against a third live tenant
before anyone spends a real application. It is **not** a substitute for Task 1
below; it cannot observe network requests or the post-submit page.

Confirmed, third tenant, same as gopuff/shieldai: `id="application-form"`,
`method="post"`, `enctype="multipart/form-data"`, 1 file input, 1 submit
button. Identity fields carry `data-qa` directly on the input
(`name-input`/`email-input`/`phone-input`/`location-input`/`org-input`) inside
minimal wrappers, exactly as the plan already described.

Three corrections/additions to what Task 2 should expect, found only because
this tenant happens to use more form features than gopuff/shieldai did:

- **Two different UUID-namespace conventions for custom questions**, not one:
  `cards[<uuid>][fieldN]` (this tenant) and `surveysResponses[<uuid>][responses]
  [fieldN]` (the earlier gopuff sample). Both match the
  `looks_like_dynamic_survey_name` regex already in `lever_selector_capture.py`
  (`/\[[0-9a-f-]{20,}\]/`) — no tooling change needed, just don't hardcode either
  prefix in the Task 2 rewrite.
- **`urls[LinkedIn]` / `urls[GitHub]` / `urls[Portfolio]`** and a **location
  field backed by an autocomplete widget** (paired hidden `selectedLocation`
  field, "Loading" / "No location found" states) — more structure than the
  gopuff sample showed. Treat as additional stable, cross-tenant fields
  alongside the core five, pending confirmation on the next tenant.
- **A hidden `h-captcha-response` field, plus a visible `div#h-captcha`
  widget.** Update after checking four tenants total (Palantir, Voltus,
  Metabase, Collate): **all four** have it. Correcting the earlier note in
  this doc — this looks like a Lever-platform default, not a per-tenant
  opt-in, though four is still a small sample. Practically this means: don't
  bother screening candidate postings for captcha-absence, there may not be
  one. It doesn't change anything about the plan — Task 3's live canary
  already assumes a human solves any CAPTCHA/MFA challenge, so this is the
  ladder working as designed, not a new blocker.

The resume-upload widget's own DOM text already contains `"Couldn't auto-read
resume."`, `"Analyzing resume..."`, `"Success!"` before any file is chosen —
suggestive that it's async, but not proof. Only Task 1's real file-select with
network observation resolves this; treat it as still open.

## Task 1: DONE — the operator ran the capture (2026-08-06)

Real capture completed against `jobs.lever.co/collate/bc4a840c-71f1-4190-8826-6b42d236e375/apply`
(the posting screened as simplest — see the screening note preserved below).
Result: **zero tripwires**, `proceed: true`. Specifically:

- The real submit is `POST` + `document` + navigating (not XHR) with
  `content_type: multipart/form-data`, to the same `/apply` URL — confirms
  the transport model this adapter already assumed.
- The resume upload did **not** fire a request during the file-selection
  phase — `ASYNC_UPLOAD` did not trip. (A separate `/parseResume` XHR was
  observed, but bucketed under the post-submit phase, not file-selection;
  the file itself travels inside the main multipart POST regardless.)
- `.capture/lever/capture.json` stays local (`.capture/` is gitignored by
  design, not an oversight here). Its `form_blank.html` — sanitized, no typed
  values, no PII — is committed directly as the new
  `tests/fixtures/lever_v1/application_basic.html`, which is the durable,
  reviewable copy of this evidence.

Screening note (preserved from before the capture ran): five real postings
were checked read-only for simplicity first; Collate came out cleanest — 0
dynamic-UUID survey fields, 1 file input, no EEO/consent language, 22 total
fields, versus 6-7 survey fields on everything else checked (Voltus,
Metabase, Palantir).

## Task 2: substantially done — evidence-based rewrite landed (2026-08-06)

`submitters/lever_v1.py` and `submitters/platforms.py` rewritten from the
real capture, not from assumption. What changed and why, in the same order
as the original task list:

- [x] `LEVER_FORM_SELECTOR` → `form#application-form`. Real markup carries no
  `data-qa`/`data-posting-id`/`data-site` on the form element at all; `_exact_form`
  no longer tries to re-verify posting identity from form attributes that
  don't exist — identity is established by the navigation that already
  happened before snapshotting.
- [x] `observe_lever_v1_fields`'s wrapper query → `li.application-question`
  (confirmed, not `capture.json`-inferred). field_id is now derived from each
  control's `name` attribute via `_field_id_from_name` (sanitized to satisfy
  `_FIELD_ID_RE`, e.g. `urls[LinkedIn]` → `urls_LinkedIn_`) rather than a
  `data-field-id` that does not exist in reality — `data-qa` exists only on
  some controls (name-input, email-input, ...) and not others that are just
  as real (the five `urls[...]` fields), so `name` is the one identifier
  every real control actually has.
- [x] Simple identity fields and the resume field: handled, proven against
  the real fixture (`tests/fixtures/lever_v1/application_basic.html`, now the
  real sanitized Collate markup, not hand-authored).
- [x] The location field's extra hidden `selectedLocation` companion control
  (an autocomplete widget backing a single logical field) — not anticipated
  in the original task list, found during the rewrite. `lever_v1_final_action_binding`
  now maps a wrapper to one field_id and recognises companion hidden controls
  sharing that wrapper, rather than treating the second named control as an
  unexplained field and blocking with `FORM_CHANGED`.
- [x] `_SYSTEM_CONTROL_NAMES` replaced wholesale: v2's assumption
  (`authenticity_token`/`csrf_token`/`_csrf`/`utf8` — a Rails convention Lever
  does not use) matched none of the real hidden fields observed
  (`accountId`, `linkedInData`, `origin`, `referer`, `timezone`,
  `socialReferralKey`, `socialSource`, `resumeStorageId`,
  `h-captcha-response`, `source`).
- [x] The visible submit button is `type="button"`, not `type="submit"` —
  a real hCaptcha-gated hidden button (`type="submit"`, no `name`, unrelated
  to the adapter's own click target) does the actual native submit.
  `lever_v1_final_action_binding`'s selector and submitter-identity payload
  updated to match.
- [x] `LEVER_V1_SELECTOR_VERSION` bumped `v2` → `v3`, and
  `submitters/platforms.py`'s matching descriptor entry updated in lockstep
  (required — `_descriptor()` refuses to instantiate the adapter on any
  mismatch between the two). Qualification tier deliberately set to
  `DRY_RUN_ONLY`, not re-claimed as `FIXTURE_QUALIFIED`: that tier means the
  *committed fixture baseline* passes, and only one of 28 fixtures has been
  migrated to real markup so far.
- [ ] Survey/EEO custom questions (Lever's `cards[<uuid>][fieldN]` /
  `surveysResponses[<uuid>][...]` patterns) — **not implemented**. Collate's
  own posting has none, so there is no completed-submission evidence to build
  this from yet, only the blank-form reconnaissance recorded earlier in this
  doc. `observe_lever_v1_fields` correctly `SELECTOR_DRIFT`s on any posting
  that has them, which is the right fail-closed behavior until real evidence
  exists — not a gap to guess through.
- [ ] Consent/attestation detection — **not implemented, and now known to
  need a different design than v2 assumed**. v2 read a `data-control-kind`
  attribute that does not exist anywhere in real Lever markup; a real consent
  question (seen in blank-form recon, not yet in a completed submission) is
  structurally just an ordinary radio question ("Yes, I consent" / "No, I do
  not consent"). Detecting it will need label-text matching, not a structural
  marker, and touches `core/form_planning.py`'s answer policy, not just this
  file. Explicitly out of scope for this pass.
- [ ] `LEVER_CONFIRMATION_SELECTOR` — **still the old, unverified v2 guess**.
  The real capture's confirmation-candidate search found no match on the
  actual post-submit page (`confirmation_selector: null` in the committed
  `capture.json`), so what real Lever shows after a successful submit remains
  unknown. Left unchanged rather than guessed.
- [x] **A significant new finding, not in the original task list, now fixed
  (2026-08-06)**: the real fixture's hCaptcha widget (`<div class="h-captcha">`,
  present on every real posting checked this session, not just this tenant)
  tripped `assess_lever_v1_snapshot`'s `CHALLENGE_DETECTED` logic, which
  treated mere presence of `.h-captcha`/`.g-recaptcha`/`iframe[src*="captcha"]`/
  `[data-captcha]` as an active, blocking challenge — meaning
  `assess_lever_v1_snapshot` could never reach `FORM` state on any real Lever
  page. Resolved by querying `.capture/lever/capture.json`'s network-request
  transcript across all three capture phases: the widget's div and its iframe
  (`newassets.hcaptcha.com/.../static/...`) are both already present at
  `form_load`, alongside only passive resources (`secure-api.js`,
  `checksiteconfig`); the actual active challenge (real puzzle images, the
  `image_label_area_select` endpoint) appears in the transcript only after the
  operator clicks submit. That's decisive evidence the four structural
  selectors are always-true false positives on real pages, not a genuine
  signal — so they were removed from `assess_lever_v1_snapshot`, keeping only
  the three text-marker checks (`"verify you are human"`, `"security
  challenge"`, `"complete the captcha"`), which still correctly catch
  `tests/fixtures/lever_v1/captcha.html` (its own heading/body text matches
  those markers independent of the structural check). The real safety
  boundary — qualification-tier gating on final execution, and the live-canary
  stage's existing requirement that a human handle CAPTCHA/MFA — is untouched;
  this only fixes a too-eager *inspection-time* heuristic. Confirmed via
  before/after test comparison: 31 failed/20 passed → 28 failed/23 passed,
  with the 3 newly-passing tests being exactly `application_basic.html`
  reaching `FORM` state plus two tests that depend on it, and the remaining 28
  failures byte-for-byte identical to the pre-fix set (the unmigrated-fixture
  issue in the follow-up list below, untouched by this change). Text markers
  alone are not proven to catch every real active-challenge presentation — no
  capture has observed the DOM during that moment — so this is evidence the
  old check was wrong, not a claim the new one is complete.
- [x] **Second finding, same day, much larger: `AnswerPolicyV1` could never
  deterministically resolve *any* real Lever field, not just custom
  questions.** Traced end to end (not assumed): every deterministic
  resolution path in `core/form_planning.py` — `_identity_value`,
  `_operator_approved`, the user-confirmed-evidence path, even the local-LLM
  path's `_allowed_llm_evidence_keys` gate — requires `field.canonical_name`
  to be set before it runs at all. `observe_lever_v1_fields` left it `None`
  for every field (no `data-canonical-name` attribute exists on real
  wrappers), a decision the v3 rewrite's own comment flagged as needing "a
  separate mechanism" later. That mechanism is now built: `lever_v1.py`'s
  new `_CANONICAL_NAME_BY_FIELD_ID` maps the real, stable field_ids
  (`name`, `email`, `phone`, `location`, `urls_LinkedIn_`, `urls_GitHub_`,
  `urls_Portfolio_`) to `core.submission_domain._CANONICAL_LABEL_ALIASES`
  keys — the same field_id → canonical-key fallback pattern
  `submitters/greenhouse_v1.py`'s `_canonical_name` already uses, not a new
  design. Every mapped field's real, *actually extracted* label was checked
  against `field_canonical_label_compatible` by direct measurement, not by
  eyeballing the HTML — because that check is stricter once canonical_name
  is set than when it's `None` (permissive `True`), a wrong mapping would
  have made resolution *worse* than doing nothing, not better. Two
  fields were caught exactly this way and are deliberately NOT mapped:
  `org` ("Current company") and `urls_Twitter_`/`urls_Other_` have no
  matching concept anywhere in `_CANONICAL_LABEL_ALIASES` at all — not
  "unmapped yet", genuinely no such concept exists in this policy (see the
  unresolved architecture gap below, which is the real consequence of this).
- [x] **Third finding, found while verifying the second: label extraction was
  silently wrong for any field whose control has rich nested UI state.**
  `observe_lever_v1_fields` took the whole `<label>` element's text; on real
  markup two fields (`resume`, `location`) wrap sibling
  `.application-field` content with the actual question inside their
  `<label>` — upload-progress status spans for resume ("Couldn't auto-read
  resume.", "Analyzing resume...", "Success!"), autocomplete dropdown chrome
  for location ("Loading", "No location found..."). The extracted label for
  resume was `"Resume/CV ATTACH RESUME/CV Couldn't auto-read resume. ..."`,
  not `"Resume/CV"` — which meant `field_is_reviewed_cv_attachment` returned
  `False` on the *one field every application needs*, silently, with no
  `SELECTOR_DRIFT` to surface it. Fixed by preferring the real, consistently-
  present `.application-label` div (confirmed 1:1 against all 11 fields in
  the real fixture) over the whole `<label>`, falling back to the old
  broader selector only if that div is absent. This is a real change to how
  `observe_lever_v1_fields` reads real markup, so `LEVER_V1_SELECTOR_VERSION`
  bumped `v3` → `v4` (with `submitters/platforms.py`'s descriptor and every
  hardcoded test string updated in lockstep, same discipline as the v2→v3
  bump) — old v3 fingerprints must not be treated as equivalent to v4 ones.
- [x] Fourth, smaller finding, same root cause as the third: even with the
  clean `.application-label` text, `"Resume/CV"` normalizes to `"resumecv"`
  (`core.sensitive_policy.normalize_policy_text` strips `/` without adding a
  space), which matched neither the `"resume"` nor `"cv"` alias alone.
  `"resume/cv"` — a real, observed label, not a guess — added to
  `_CANONICAL_LABEL_ALIASES`'s `resume`/`resume_upload`/`cv`/`cv_upload`
  entries. Narrow, additive, doesn't touch matching logic.
- [x] Verified cumulative effect empirically at every step (`python -c
  "..."` against the real fixture + `AnswerPolicyV1.plan_fields` directly,
  not just re-running pytest) rather than assuming the fixes composed
  correctly: before this pass, all 11 real fields abstained and
  `ready_for_permit` could never be reached; after, `resume`/`name`/`email`
  resolve with real, inspectable provenance
  (`verified_attachment`/`deterministic_identity`), and `phone`/`location`/
  `urls_LinkedIn_`/`urls_GitHub_`/`urls_Portfolio_` correctly abstain for a
  mundane, expected reason (the fake test profile has no data for them, not
  a mapping gap — a populated profile resolves them too). `org`/
  `urls_Twitter_`/`urls_Other_` still correctly abstain, for the real
  architecture reason below.
- [x] **Reviewed optional blanks are now supported safely.** The shared
  `AnswerDecisionV1` contract already provided the
  `OPERATOR_CONFIRMED_BLANK` disposition; Lever's browser transport now
  accepts it only for an exact observed optional, non-sensitive, non-file,
  non-consent/attestation control with no minimum length. `fill()` leaves that
  control untouched, and the final-action proof binds the empty value without
  allowing a checked control, selected option, or non-empty value. Required,
  sensitive, unknown, and attachment controls still block. The sanitized DOM
  rehearsal covers the real `org`, Twitter, and Other optional fields and
  remains non-submitting; custom survey controls and final live qualification
  are still intentionally out of scope.
- [x] `ruff check .`, `ruff format --check .` — clean on every touched file.
  Full suite (`pytest tests/ -q`, the reliable run — a first attempt piped
  through `tail -50` before backgrounding and silently discarded the earlier,
  alphabetically-first failures, producing a misleading 90-failed count;
  rerun with complete output redirected straight to a file instead): **26
  failed, 2941 passed, 20 skipped** — down from this session's 33-failing
  starting point (after the earlier v2→v3 rewrite + hCaptcha fix). Every one
  of the 26 is accounted for: 19 in `test_lever_v1_fixtures.py` + 1 in
  `test_lever_browser_v1.py` (the pre-existing, already-scoped unmigrated-
  fixture backlog, follow-up item 1), 2 in `test_adapter_qualification_matrix.py`
  (follow-up item 2's mixed-tier issue), 1 in `test_lever_v1_qualification_report.py`
  (the deliberately-stale Lever report), `test_webhook_ingest_text.py` and
  `test_control_plane_runner_scripts.py`'s one each (pre-existing/unrelated,
  confirmed via `git stash` comparison earlier this session) — and one new,
  real, narrow, and fully understood side effect:
  `test_v4_local_model_qualification.py::test_committed_local_model_report_is_aggregate_only`
  now fails because `scripts/evaluate_v4_local_model_qualification.py`'s
  `_SOURCE_FILES` set includes `core/submission_domain.py`, and its committed
  report's `source_integrity` hash no longer matches that file's content
  after the `resume/cv` alias addition above — the exact same
  "re-earn qualification after the source changed" situation this doc
  already describes for the Lever report, just for a different, unrelated
  qualification (a real local-model evaluation, `real_local_model: true`
  over 410 real cases — not something to trigger casually or fake by
  hand-editing the committed hash). Left honestly failing rather than
  silently patched.

### Task 2b: the 24-fixture backlog cleared, Lever re-promoted to FIXTURE_QUALIFIED (2026-08-09)

**Status update (2026-08-10): the label-only consent gap is resolved in
`codex/lever-consent-semantics`.** Lever observation now recognizes narrowly
bounded privacy/consent and attestation label phrases even when the control is
an ordinary radio or checkbox and `data-control-kind` is absent. The observed
field is typed as `CONSENT`/`ATTESTATION` and still requires operator review.
Because the existing executor accepts only boolean checkbox controls for these
types, a label-only radio is deliberately quarantined rather than guessed or
submitted. A sanitized label-only radio regression covers the previously
missing path; generic preference labels remain ordinary controls.

Follow-up items 1 and 2 from the prior pass, done in one sweep the same day:

- [x] All 24 remaining `tests/fixtures/lever_v1/*.html` fixtures migrated or
  retired, per the categorization reconnaissance from the prior pass:
  - **Real markup + one labeled mutation, no new evidence needed**
    (`wrong_method.html`, `disabled_submit.html`,
    `unreviewed_hidden_control.html`, the four `outer_*` actionability
    fixtures, `multiple_resume.html`, `duplicate_field.html`): rebuilt from
    `application_basic.html`'s actual structure with exactly one deliberate
    change each. `duplicate_field.html`'s old premise (`data-field-id`
    collision) doesn't exist under v3+ at all — rebuilt as the real
    equivalent, two wrappers whose controls share one `name` (the
    `field_id in seen_ids` check `observe_lever_v1_fields` still has).
  - **`outer_has_proxy_guard.html`**: same treatment, but keeping the real
    hidden system fields instead of `authenticity_token` (proven fictitious
    this session) — the fixture's whole point is proving a CSS `:has()`
    guard doesn't fool static actionability checking, so it must actually
    reach and pass `lever_v1_final_action_binding`, not just `FORM` state.
  - **Retired outright**: `invalid_action.html` — its premise (an
    action-URL mismatch check) was deleted in the v3 rewrite, confirmed by
    that code's own comment; migrating it would mean re-adding a check the
    rewrite intentionally removed. Fixture deleted, its
    `test_unreviewed_or_invalid_final_boundary_fails_before_plan`
    parametrize entry removed, fixture count 28 → 27 everywhere that counts
    it.
  - **Explicitly hypothetical, real wrapper shape + honestly-labeled
    guessed content** (`application_custom_select.html`,
    `application_radio_checkbox.html`, `application_consent.html`): none of
    these scenarios appear in the one real capture. Each fixture carries an
    HTML comment stating exactly what's evidence-backed (the
    `li.application-question` wrapper shape; the `cards[<uuid>][fieldN]`
    survey-question naming convention, confirmed via the earlier blank-form
    recon) versus what's a plausible construction. `application_consent.html`
    is the weakest of the three on purpose, documented as such: blank-form
    recon found real consent questions are structurally ordinary radio
    questions, not a `data-control-kind="consent"`-bearing control, meaning
    `_control_type()`'s only detection mechanism likely didn't match real
    Lever markup at all — a live, safety-relevant gap. The follow-up now adds
    bounded label semantics in the Lever observer, so a label-only consent
    question is typed as `CONSENT` and cannot skip the mandatory
    operator-review path. The fixture remains hypothetical; the code change
    is covered by a sanitized label-only regression and does not claim a live
    ATS scope.
  - **Also migrated, found along the way**: `prompt_injection.html` was
    completely unreferenced by any test (confirmed by grep) despite being a
    committed fixture — migrated to real markup and given an actual test
    (`test_hostile_question_label_is_observed_and_flagged_for_review`)
    proving `field_requires_operator_review` catches its hostile label,
    closing a real, previously-silent test-coverage gap.
- [x] **A fifth selector-version-worthy finding, found migrating
  `outer_content_visibility.html`**: `_visible()` checked
  `display:none`/`visibility:hidden`/`opacity:0` but not
  `content-visibility:hidden` — a standard CSS property that also fully
  hides an element, and one `_static_actionability_capture` *already*
  recognized a few lines below for actionability purposes. Inconsistent,
  not evidence-dependent (a CSS-semantics fix, not a markup-shape claim),
  and a real gap: a page (or an injected wrapper) hiding a form this way
  would previously have been read as visible. Fixed by adding the marker to
  `_visible()`'s existing style check. `LEVER_V1_SELECTOR_VERSION` bumped
  `v4` → `v5` (same lockstep-update discipline as v2→v3 and v3→v4).
- [x] **Lever re-promoted `DRY_RUN_ONLY` → `FIXTURE_QUALIFIED`**
  (`submitters/platforms.py`): the tier's own definition — "the committed
  fixture baseline passes" — is now honestly met; all 27 fixtures pass
  against the real v5 contract. `allows_live_submission`/
  `allows_final_execution` remain gated on `LIVE_CANARY_QUALIFIED` and an
  exact `qualified_form_scope`, neither of which this touches — no change
  to what's actually allowed to execute, only to what evidence-backed claim
  the tier makes.
- [x] `docs/qualification/lever-browser-v1.json`/`.md` regenerated for
  real: every fixture's SHA-256 and `assess_lever_v1_snapshot` result
  recomputed directly from the current files and current code (not
  hand-typed), fixture manifest digest recomputed the same way the test
  (`test_report_binds_exact_fixture_bytes_states_and_manifest`) verifies it.
  Every fixture's expected state/reason came out identical to the old
  (stale-version) report's claims — the migration didn't change any
  documented behavior, only made the claim honestly earned again.
  `scripts/build_adapter_qualification_matrix.py --write` regenerated
  `docs/qualification/adapter-matrix.{json,md}` the proper way (a
  `--write` mode already existed; not hand-edited). Needed one small,
  evidence-independent script fix along the way: the markdown validator's
  fixture-count → spelled-out-word map (`{9: "Nine", 13: "Thirteen", ...}`)
  had no entry for 27, unconditionally rejecting any markdown with that
  count regardless of content — added `27: "Twenty-seven"`.
- [x] Full Lever-adjacent suite (`test_lever_v1_fixtures.py` +
  `test_lever_browser_v1.py` + `test_adapter_qualification_gate.py` +
  `test_application_response_form_plan_contract.py` +
  `test_submission_evidence_contract.py` +
  `test_lever_v1_qualification_report.py` +
  `test_adapter_qualification_matrix.py`): **98 passed, 0 failed**. Full
  repo suite (`pytest tests/ -q`, complete untruncated output, ~17 min):
  **3 failed, 2964 passed, 20 skipped** — down from 26 failed/2941 passed at
  the start of this pass. Every one of the 23 resolved failures was
  Lever/qualification-related; the 3 remaining are `test_control_plane_runner_scripts.py`
  and `test_webhook_ingest_text.py` (pre-existing/unrelated, confirmed via
  `git stash` comparison earlier this session) and
  `test_v4_local_model_qualification.py` (follow-up item 6 below, already
  understood, not fixed here). `ruff check .`/`ruff format --check .` clean
  on every touched file.

### Task 2c: the "operator-reviewed-blank" blocker turned out not to need a redesign (2026-08-09, same day)

**Correction to follow-up item 5 as it stood a few hours earlier the same
day**: that entry said resolving `org`/`urls_Twitter_`/`urls_Other_`'s
permanent abstention needed a new domain-model concept — "an operator
reviewed this exact field and confirmed leaving it blank" — in shared
`core/submission_domain.py`/`core/form_planning.py` code used by all five
adapters. Pushed to look harder rather than accept that conclusion, and it
was wrong, for a fixable reason: it was reached by treating every
non-`RESOLVED` decision as one undifferentiated "not answered" bucket, when
the domain model already distinguishes two: `AnswerDisposition.ABSTAINED`
(no answer available, and `_abstain()` only assigns this when the field is
*neither* `required` *nor* sensitive) versus `AnswerDisposition.OPERATOR_REQUIRED`
(assigned when it's required or sensitive — an existing, already-tested
mechanism `greenhouse_v1.py`/`workday_v2.py` already rely on).
`AnswerPolicyV1.plan_fields`'s own blocker computation already only escalates
`REQUIRED_FIELD_UNKNOWN` for `field.required` fields — `policy.blockers` was
correct and empty the whole time (confirmed directly, `blockers: ()`, in the
diagnostic script from the earlier pass). The actual bug was narrower and
entirely inside `submitters/lever_v1.py`: `LeverBrowserV1.inspect()` and
`preflight()` each additionally blocked on `any(decision.disposition is not
RESOLVED for decision in policy.decisions)`, collapsing the harmless
`ABSTAINED` case into the same bucket as a genuine missing required answer.
`greenhouse_v1.py`/`workday_v2.py` were already checked earlier this session
and confirmed to trust `policy.blockers` alone at the equivalent point — a
second read of `greenhouse_v1.py:1698-1738` (this pass) confirmed the exact
matching pattern in more detail (filter to `RESOLVED` decisions, check only
that `required_ids.issubset(...)`, no blanket check). Removed the extra
check from both places, matching that already-proven pattern instead of
inventing a new one. `AnswerDisposition` import dropped from `lever_v1.py`
(both usages were the removed checks).

Verified, not assumed: `test_inspection_builds_auditable_ready_plan_and_never_clicks`
(renamed back from `..._plan_and_never_clicks` now that `ready_for_permit`
is genuinely `True` again) confirms `ready_for_permit is True` and
`blockers == ()` against the real fixture and the same sparse fake profile
used throughout this file — no test data changes, only the adapter fix. The
`_assume_ready` test helper (four other browser tests) is gone entirely,
not just unused — those tests now exercise the real, unmodified `inspect()`
→ `preflight()` flow, which is more honest than a synthetic placeholder-
decision workaround. Full Lever-adjacent suite re-run after: still 98
passed, 0 failed.

`application_consent.html`'s detection-mechanism gap is resolved separately in
the follow-up noted under Task 2b; this section only retracts the specific
"needs a new domain-model concept" claim for the
`org`/`urls_Twitter_`/`urls_Other_` case.

### Task 2d: the real remaining blocker for Task 3 — `lever_playwright.py` was never rewritten (2026-08-09, found investigating the fix above)

**Status update (2026-08-10): resolved in `codex/lever-playwright-v2`.** The
Playwright transport now reuses the evidence-backed v5 wrapper, field-id,
system-control, upload, form, and submit-button contract. A sanitized
Chromium rehearsal exercises the real DOM with `requestSubmit` stubbed and
asserts that no POST leaves the browser. This does not qualify a live ATS
scope; the real-URL dry run and canary remain mandatory.

**This is now the single most significant open finding in this document.**
Every fix this session — v2→v5, the fixture backlog, the `ready_for_permit`
correction above — touched only `submitters/lever_v1.py` (pure HTML parsing
and business logic, exercised by `_FakeSession`-backed tests) and
`submitters/platforms.py`. `submitters/lever_playwright.py` — the actual
Playwright browser driver `LeverBrowserV1` would use in a real dry-run or
live canary — duplicates several of the same markup assumptions v2 already
disproved, and none of those duplicates were ever updated:

- `_SYSTEM_CONTROL_NAMES = frozenset({"authenticity_token", "csrf_token",
  "_csrf", "utf8"})` (line 65) — the exact fabricated Rails-convention list
  replaced in `lever_v1.py` with the real observed hidden fields, still
  present here verbatim, unfixed.
- `fill()` (line 1102) locates each field with
  `[data-qa="application-field"][data-field-id="{field.field_id}"]` (line
  1126) — real markup has neither attribute; this would find zero elements
  and raise `FORM_CHANGED` on the very first field of any real page.
- `_FORM_PROOF_SCRIPT` (lines 300-759, ~460 lines) — a large embedded
  JavaScript re-verification script, evaluated directly in the browser page
  immediately before the irreversible click, that re-implements a big slice
  of `lever_v1.py`'s field-observation and actionability-capture logic
  independently, in JS, for atomicity against the click. It has its own
  parallel copies of the form selector (`'form[data-qa="application-form"]
  [data-posting-id][data-site]'`, lines 408/430), the submit-button selector
  (`'button[data-qa="btn-submit"][type="submit"]'`, lines 418/437 — doubly
  wrong, since real markup's visible submit button is `type="button"`, not
  `type="submit"`, confirmed this session), the field-wrapper selector
  (`'[data-qa="application-field"][data-field-id]'`, line 477), and a
  `data-field-id`-reading field-id lookup (line 522) — none matching real
  markup. `_file_observation` (line 992/998) and `ensure_resume_attachment`
  (line 1024/1039, keyed on a `data-canonical-name="resume"` wrapper
  attribute that doesn't exist) carry the same pattern.
- Net effect: even with every other fix in this document, `preflight()`
  calling into a real `PlaywrightLeverCandidateSession` against a real page
  would fail immediately — not on some edge case, on the very first
  structural check. Task 3 (fixture-qualify → real-Chromium rehearsal →
  real-URL dry run → live canary) is unreachable until this file is rebuilt
  from the same real evidence `lever_v1.py` already was.

**Deliberately not attempted in this pass.** This is qualitatively
different from every other fix today: `lever_v1.py`'s rewrite could be
verified against real captured HTML with plain pytest; this file's
correctness can only be verified by actually running it against a real
Playwright page (`test_lever_playwright_safety.py`'s existing tests cover
network-guard and multipart-commitment *properties*, not the markup
selectors themselves, and don't exercise a real DOM at all). Rewriting
~500 lines of markup-dependent Playwright/JS code — including a
security-critical, embedded re-verification script that runs immediately
before an irreversible click — without the ability to verify it against a
real browser is exactly the "confident but unverified" work this whole
project rebuild exists to prevent. Needs its own dedicated pass, ideally
informed by an actual Chromium rehearsal (Task 3's own second step) rather
than done blind and then discovered wrong at that step anyway.

### Follow-up (not done in this pass, scoped so the next session can pick it up)

1. ~~Migrate or retire the 24-fixture backlog~~ — done 2026-08-09, see
   Task 2b above.
2. ~~Regenerate the stale qualification report / fix the qualification
   matrix mixed-tier failure~~ — done 2026-08-09, see Task 2b above.
3. ~~Investigate the hCaptcha/`CHALLENGE_DETECTED` finding above~~ — done
   2026-08-06, see the checked item above.
4. Investigate `LEVER_CONFIRMATION_SELECTOR` — ask the operator what the real
   post-submit page showed (screenshot or plain description), since the
   capture tool itself found no match. Still open, still needs the operator
   — `capture.json` only stores network-request metadata per phase
   (`content_type`/`method`/`phase`/`resource_type`/`url`), not DOM/HTML
   snapshots, so there's nothing further to extract from the existing
   capture. One thing checked this pass that *is* useful, though:
   `confirmation_url_path` in the committed `capture.json` equals the exact
   apply URL, not a distinct URL — Lever's real confirmation state is not a
   navigation/redirect, it's an in-place DOM update on the same page. Rules
   out any URL-based detection approach; confirmation detection has to stay
   DOM-content-based, which is already the current design.
5. ~~Design "operator reviewed and confirmed leaving blank" in shared
   domain code~~ — retracted, see Task 2c: the real bug was narrower and
   Lever-local, and is fixed. The `application_consent.html` detection-
   mechanism gap is handled by the bounded label-only semantic
   classification noted in Task 2b (2026-08-10).
5b. ~~**The real top priority for Task 3 now**: rebuild
   `submitters/lever_playwright.py` from the same real evidence
   `lever_v1.py` was rewritten from — see Task 2d above for the full list
   of stale locations. Needs a real-Chromium rehearsal to verify, not just
   unit tests; likely the right way to sequence this is to treat it as part
   of Task 3's own second step rather than a separate pre-step.~~ — done
   2026-08-10; the sanitized rehearsal is committed in
   `tests/test_lever_playwright_dom_rehearsal.py`.
6. Re-earn `scripts/evaluate_v4_local_model_qualification.py`'s committed
   report (`test_v4_local_model_qualification.py::test_committed_local_model_report_is_aggregate_only`
   fails on source-integrity, not logic — see above): unrelated to Lever, a
   side effect of `core/submission_domain.py` being one of that
   qualification's tracked source files. Needs an actual local-model
   evaluation run (`real_local_model: true`), not a quick fix — left
   honestly failing rather than faked.

### Task 3: Fixture-qualify, then dry-run, then the live canary — **operator present throughout**

Unchanged from the design spec's existing ladder (§2 qualification stages) —
not rewritten here because nothing learned this session changes the ladder
itself, only what has to happen before Task 3 can start. As of 2026-08-10:
the hCaptcha false-positive, the fixture backlog, the `ready_for_permit`
over-blocking bug, and the stale Playwright transport contract are fixed
(Tasks 2, 2b, 2c, and 2d above). The remaining gates are deliberately
operator-present: a different real-URL dry run, then one explicitly approved
live canary with manual CAPTCHA/MFA handling.

- [x] Offline fixture suite passes against the rewritten contract.
- [x] `submitters/lever_playwright.py` rebuilt from real evidence (Task 2d).
- [x] Real-Chromium rehearsal with `HTMLFormElement.prototype.submit` stubbed —
  confirms no request leaves before spending a real application on it. Also
  the natural point to verify Task 2d's rewrite against a real page.
- [x] Guarded one-URL read-only selector inspection command added in
  `scripts/lever_dry_run_smoke.py`; it requires dry-run mode and operator auth,
  records only redacted field types/reasons, and never fills, uploads, clicks,
  or creates qualification authority.
- [ ] One real-URL dry run (`DRY_RUN=true`) against a **different** real Lever
  posting than the one captured, to catch overfitting to a single tenant's
  markup.
- [ ] One live canary — operator selects and approves the exact job,
  handles CAPTCHA/MFA manually, confirms via the employer's own confirmation
  email. This is the step that actually produces the P1 exit criterion.

---

## Exit Criteria (unchanged from the design spec's P1, restated for this adapter)

- [ ] One `Submission` with `outcome == "confirmed_submitted"`
- [ ] A `SubmissionEvidence` row satisfying `ck_submissions_confirmed_evidence`
- [ ] A `LIVE_CANARY_QUALIFIED` `AdapterQualificationRecord` for Lever
- [ ] `ruff check .`, `ruff format --check .`, `pytest -q` all pass

## What this plan deliberately does not do

- Does not write Task 2's selector contract now. Doing so before Task 1's
  output exists means guessing at the exact `wrapper_selector`/`data-qa`
  values, which is the fabrication this whole rebuild exists to prevent —
  the P0 execution notes already record what guessing here costs (twenty-two
  green fixtures against assumed Greenhouse markup that couldn't read a real
  page).
- Does not touch the safety switches (`DRY_RUN`, `DRAFT_ONLY`,
  `FINAL_SUBMIT_ENABLED`, `LIVE_AUTOMATION_ACKNOWLEDGED`) at any step. Task 3's
  dry run and live canary use the existing gated mechanisms exactly as designed,
  not a bypass.
- Does not resurrect the Greenhouse capture as a parallel effort. It's kept in
  the repo, tested, and correct for when a real transport rewrite is scoped —
  running it before then just spends effort re-confirming a finding already
  established for free via `curl`.
