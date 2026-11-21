# Record and publish each application

An active browser executor records each observed page, field answer, verified
autofill, blank optional field, CV upload, navigation, submit attempt, result and
blocker through `scripts.log_browser_activity`. Keep the exact observed and entered
values separate, with timestamps and sanitized form snapshots. Record the final
review's field labels in an observation event's `details.reviewed_fields` list.
Never record passwords, verification codes, cookies or authentication tokens.

After each application outcome, including blocked and uncertain attempts:

1. Save the receipt or blocker, record the truthful ledger outcome, and refresh
   the run report. Close the application page and record the navigation.
2. Run `python -m scripts.checkpoint_application checkpoint --config
   '<PRIVATE_CONFIG>' --job-url '<OBSERVED_JOB_URL>'` in the isolated publishing
   checkout. A completed application requires answer coverage, a matching CV
   upload, a submit event, and confirmation evidence.
3. The command makes a consistent SQLite backup, includes the application steps,
   exact CV, referenced evidence, run artifacts and verified profile sources,
   verifies the bundle, and encrypts it with AES-256-GCM using a fresh nonce.
4. Only ciphertext and generic ciphertext metadata enter the Git commit. The
   command pushes the dedicated branch, fetches the new LFS object into a fresh
   private cache, checks its hash, authenticates/decrypts it and verifies every
   archived file. Its private receipt records the verified remote commit.
5. Run `guard --config '<PRIVATE_CONFIG>'` before the next application. A failed
   or pending publication blocks continuation. Retry the checkpoint, never the
   job submission. An interrupted preparation requires inspection.

The private JSON configuration supplies `source_root`, `run_dir`, `run_id`,
`archive_worktree`, `private_dir`, `key_file`, `branch`, and `expected_origin`.
Use a clean isolated `codex/` branch, a key outside both checkouts, and staging
under the execution checkout's ignored `runtime-data/`, outside the run folder.
The private config, key, plaintext and verification downloads must not be committed.

Run `bootstrap --config '<PRIVATE_CONFIG>'` once before continuation. It archives
the current run and all catalogued CVs, preserving historical gaps explicitly.
It cannot reconstruct answers or steps that were never captured. Later capsules
include their selected CV and a full ledger snapshot; retain earlier archives
for other historical evidence. Restoring never overwrites a live ledger.

This is an active-session workflow, not a background job submitter. The executor
must observe and log the UI; the command cannot infer missing browser actions.
Publishing to a branch does not merge it into the default branch.
