# Lever Browser v1 Qualification

Recorded: 2026-08-09

Achieved tier: `fixture_qualified`

This artifact records sanitized offline fixture qualification for the `lever`
adapter `1.0.0`, selector `lever-candidate-v5`, using the `two-phase-v2`
execution contract.

Re-earned the same day as v3 through v5: the selector contract was rewritten
from a real, completed submission (jobs.lever.co/collate), which disproved
v2's markup assumptions entirely. All twenty-seven fixtures were rebuilt
against the current contract — real markup plus one labeled mutation each
where the scenario doesn't require Lever-specific evidence, explicitly
hypothetical where it does (a custom select/radio/consent question, none
observed on the one real submission) — rather than left claiming a tier the
evidence no longer supported. See
`docs/superpowers/plans/2026-08-05-p1-lever-first-capture-and-selectors.md`
for the full evidence trail.

## Evidence

- Twenty-seven sanitized HTML fixtures cover exact candidate identity, standard
  and custom controls, selected-CV attachment, consent, login, MFA, challenge,
  closed and already-applied states, selector drift, duplicate/ambiguous
  fields, unreviewed controls, prompt injection, outer-wrapper actionability,
  a CSS `:has(...)` mutation guard, and exact visible confirmation.
- Fixture manifest digest:
  `a6daab38f3826bec1f083653e1a9c6102729d8a850567ee0d93898bca6aba8ff`.
- Adversarial tests bind every reviewed decision and the selected CV bytes to
  the exact native multipart payload.
- No external network request or irreversible final action was performed.

## Remaining gates

- Real-URL dry run: pending.
- Live canary: pending.
- Qualified live form scope: empty.
- Final external action: disabled.

Fixture qualification cannot authorize live submission. A later tier requires
its own exact reviewed evidence, a non-empty form-fingerprint scope, and a new
selector version if the real candidate form differs from these fixtures.

## Privacy boundary

The report stores only sanitized fixture identifiers, bounded state and reason
codes, and cryptographic digests. It contains no employer location, candidate
identity, application answer, CV content, browser state, page content, or
authentication data.
