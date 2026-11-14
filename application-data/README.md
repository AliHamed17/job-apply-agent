# Encrypted application archive

This public repository stores the applicant's files as **AES-256-GCM authenticated
ciphertext**, split into Git LFS objects. The decryption key is held separately by
the owner. It is not in Git, in the public manifest, or inside the archive.

The encrypted bundle contains the application ledger and its ordered activity,
profile/fact sources, current and historical CV collections, reports, browser
evidence, saved session workflows and source snapshots. The complete file list,
source paths, SHA-256 hashes, snapshot times and exclusions are inside the archive.
Public manifests describe only encrypted objects.

Credential stores, authenticated browser profiles, signing/decryption keys,
dependencies and transient caches are excluded where identified by the backup's
explicit path rules. The manifest records the exact exclusions. The archive is a
snapshot, not a running database or proof that an application was submitted.

## Restore for Codex

Install Python 3.11+, `cryptography`, and Git LFS. Clone this repository, then:

```powershell
git lfs pull --include='application-data/*'
python -m scripts.encrypted_application_archive verify --input application-data/session-data-20261114.json --key-file '<PRIVATE_KEY_FILE>'
python -m scripts.encrypted_application_archive restore --input application-data/session-data-20261114.json --key-file '<PRIVATE_KEY_FILE>' --output-dir '<NEW_PRIVATE_FOLDER>'
```

`verify` checks part sizes/hashes, authenticates the whole encrypted stream and
verifies every restored file against its private inventory. `restore` performs
the same checks before writing to a new destination. A wrong key or modified
archive fails; existing keys, files and restore directories are not overwritten.

Read `CODEX_START.md` in the restored directory, then the archived private handoff
and [CODEX_HANDOFF.md](../CODEX_HANDOFF.md). Map restored paths deliberately and
reconcile newer application history before using any ledger snapshot. Preserve
uncertain outcomes, employer exclusions, source-bound answers and CV identities.
Never turn an old in-progress claim into an automatic retry.

The current browser session and its cookies are not backed up. Connect/sign in
normally in the new Codex session under current authorization. Restoring files
does not start a browser, model, background service or submission run.

## Key custody and future snapshots

`session-update-20261114.json` is a separately encrypted incremental snapshot
using the same separately held owner key and a fresh nonce. It includes the
selection recording, the timed-run report/evidence, a newer consistent ledger,
and source/data files added or changed after the base capture began. Its private
manifest identifies the base archive and the exact capture limits.

Restore the update to a **second new private folder** using the same command
with `--input application-data/session-update-20261114.json`. Read its
`CODEX_UPDATE.md`. Compare its newer files with the base before choosing the
offline working dataset. Never overlay either snapshot onto a live ledger or
overwrite newer work. Authentication/session stores remain excluded.

`session-latest-20261114.json` adds a subsequent consistent ledger backup, a
fresh queryable handoff database, the active CV collection, and the later batch's
report and receipt evidence. Restore it to a **third new private folder** using
the same owner key, then read `CODEX_LATEST.md`. Its private inventory records
capture times and the exact contents. The earlier archives retain the original
source material and history; this update does not replace or delete them.

Keep a separate secure backup of the key. Losing every copy makes the encrypted
data unrecoverable; publishing the key would disclose the archive. Never paste it
in a chat, commit, issue, CI variable, or public release.

The offline helper supports `create-key`, `encrypt`, `split`, `verify` and `restore`.
`encrypt` accepts a reviewed ZIP containing a `jobapply-private-bundle-v1` inventory
named `bundle-manifest.json`; every file must have a matching byte count and SHA-256.
Create the plaintext ZIP in a private staging directory outside the public clone.
Use `split --part-size-mib 512` to produce bounded Git LFS objects. Authenticate
and verify the uploaded copy before declaring a backup complete.

The encrypted data is excluded from Docker image builds. The existing Vercel
allowlist also excludes this directory. No decryption key is sent to either
deployment environment or GitHub Actions.
