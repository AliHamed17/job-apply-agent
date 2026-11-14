# Facts that blocked applications

`scripts.facts_questionnaire` reads the browser-batch ledger and shows which missing
facts held jobs back, ranked by how many jobs each one affects. It is offline and
read-only: no network, browser, LLM, or ledger writes. It costs nothing to run.

```powershell
python -m scripts.facts_questionnaire report --ledger runtime-data/browser-batch.sqlite3
python -m scripts.facts_questionnaire build  --ledger runtime-data/browser-batch.sqlite3 --out-dir runtime-data/facts
# fill in runtime-data/facts/facts-questionnaire.json, then:
python -m scripts.facts_questionnaire apply  --questionnaire runtime-data/facts/facts-questionnaire.json --out-dir runtime-data/facts
python -m scripts.export_verified_answer_cache --cv-dir '<CV directory>' --verified-facts runtime-data/facts/reviewed-facts.json --output runtime-data/verified-answer-cache.json
```

Outputs contain employer names from your ledger, so they stay under the ignored
`runtime-data/` directory. They never contain job URLs, titles, or descriptions.

## What each gap kind means

| Kind | Meaning | Can a confirmed answer unblock it? |
|---|---|---|
| `experience` | The form asked for exact years with a named technology and none is on file | Yes, and the answer cache accepts it today |
| `fact` | Notice period, salary, hybrid/commute, certifications, travel, postal code | Only after a policy decision; the cache has no key for these |
| `consent` | A required Terms or privacy checkbox | Only after a consent policy decision |
| `legal` | National ID, clearance, military service, work authorisation | No; these are never automated |
| `fit` | A stated minimum or degree the profile does not meet, or an off-profile role | No; answering does not change the requirement |
| `obstacle` | CAPTCHA or human check, account or code, closed or duplicate, broken page or upload | No |

An unmet minimum is deliberately **not** an `experience` gap. "Requires 4+ years" with a
profile of one year is a fit problem, and entering a number cannot fix it. A text that
names an experience gap but no technology is reported as `fit` for a manual look.

## How answers reach the answer cache

`apply` validates each answer, never guesses a blank one, and writes:

- `facts-questionnaire.confirmed.<hash>.json`: an immutable copy of your answers, used as
  the hashed proof. A later edit creates a new file; earlier proofs stay valid.
- `reviewed-facts.json`: `experience.<skill>_years` entries in the schema that
  `export_verified_answer_cache --verified-facts` expects, with source type
  `user_confirmation` and scope `years of professional experience with <skill>`.
  A consumer must look a fact up with that exact scope.
- `confirmed-preferences.json`: preferences and the consent choice, marked
  `used_by_answer_cache: false`. Nothing reads it until that category is approved.

If a proof file changes after the cache is built, the cache disables every answer
instead of serving a stale one.

## Limits

- Classification is keyword based on the text the executor recorded. The estimate of
  unblocked jobs counts a job only when every one of its gaps is a fact or consent you
  can supply, so it errs low. It is a planning number, not a guarantee.
- A gap counted for a job may sit beside others: the questionnaire shows how many of a
  skill's jobs are also held back by something else.
- To add a gap, add a pattern in `batch_apply/facts_gap.py` and a phrasing in
  `tests/test_facts_gap.py`.
