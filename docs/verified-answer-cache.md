# Verified answer and CV cache

An active LLM executor can export a private cache once, reuse exact scoped facts,
and consult earlier CV selections while reviewing new jobs. The cache keeps source
fingerprints and provenance pointers. It does not operate a browser or grant
permission to apply, accept terms, solve challenges, or reuse a terminal job.

Run from the repository root:

```powershell
python -m scripts.export_verified_answer_cache --cv-dir '<CV directory>' --verified-facts runtime-data/reviewed-facts.json --output runtime-data/verified-answer-cache.json
```

The SQLite connection uses read-only mode. The exporter reads whitelisted contact
fields from the verified overlay and the configured locality from the profile.
It deliberately bypasses the profile loader's attachment/text extraction. Only
unchanged PDFs inside the supplied CV directory with a verified single page enter
the historical CV route list. Install the existing `pdf` extra for page counting.
The exporter reads page metadata, never CV text; this is not a content/privacy
certification. A fresh job/CV review and the normal claim workflow remain required.

Additional experience answers must be supplied as a separately reviewed private
manifest. No answer values, identities, actual employer cases, or source paths are
preconfigured. For example, this **synthetic** entry illustrates the schema:

```json
{
  "facts": [{
    "id": "reviewed-query-answer",
    "key": "experience.query_language_years",
    "value": 2,
    "scope": "exact reviewed question about query-language experience",
    "reviewed": true,
    "inherited_prefill": false,
    "source_type": "user_confirmation",
    "proofs": [{"path": "<local reviewed evidence>", "sha256": "<SHA256>"}]
  }]
}
```

Use `recording_active_answer` only after reviewing an actual active entry, with
`active_entry_observed: true` and at least two distinct source proofs, such as the
original recording and a reviewed frame. Fingerprints preserve that review; the
exporter cannot interpret a recording or establish that a declared fact is true.
An inherited form prefill, posted job requirement, submitted application, or CV
selection is not a reviewed answer. General tenure never supplies a missing
technology duration. Keep salary, notice, travel and other unsupported answers
unfilled. Unknown categories retain blocker event pointers without copying raw
form fields, credentials, identifiers, or source text.

Load and use the cache with an exact fact key and scope:

```python
from batch_apply.answer_cache import load_answer_cache, lookup_answer

cache = load_answer_cache("runtime-data/verified-answer-cache.json")
fact = lookup_answer(cache, "experience.query_language_years",
                     scope="exact reviewed question about query-language experience")
if fact is not None:
    # Inspect provenance and confirm the actual form question has this exact meaning.
    answer = fact.value
```

Loading checks all source hashes and binds values back to their source selectors.
Any missing/changed source disables answer lookup until the cache is regenerated.
Historical routing examples carry selected CV hashes and matching upload event IDs;
an employer receipt does not prove that an applicant meets every requirement.
Keep cache outputs and provenance manifests under ignored `runtime-data/`, and
keep personal profiles, CVs, recordings, credentials and application artifacts out
of public releases. CLI logs contain counts only.
