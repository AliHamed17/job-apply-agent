"""Export a private, queryable handoff without changing the live application ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SOURCE_FILES = {
    "user_profile.yaml": "profile_unverified",
    "cv_routing.yaml": "cv_routing",
    "runtime-data/verified-profile-overlay.json": "verified_profile",
    "runtime-data/verified-answer-facts.json": "reviewed_facts",
    "runtime-data/verified-answer-cache.json": "answer_cache_snapshot",
    "runtime-data/application-knowledge.json": "knowledge_snapshot",
    "runtime-data/employer-policy.json": "employer_policy",
    "runtime-data/browser-batch-state.json": "state_snapshot",
}
ASSET_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".html",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".mp4",
    ".py",
    ".toml",
    ".patch",
    ".bak",
    ".bin",
}
SECRET_PARTS = {
    ".env",
    ".env.local",
    ".browser_profiles",
    ".browser_state",
    ".browser-state",
    ".linkedin_profile",
    ".portal_profiles",
    ".job-agent",
    "sessions",
    "session-data",
    ".git",
    ".wwebjs_auth",
    ".gmail_oauth.json",
}


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _path(root: Path, raw: str) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else root / path).resolve()


def _fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _references(value):
    """Collect only explicit evidence references, never arbitrary strings or URLs."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"artifact", "screenshot_path"} and isinstance(item, str) and item:
                yield item
            elif key == "artifact_paths" and isinstance(item, list):
                yield from (path for path in item if isinstance(path, str) and path)
            elif isinstance(item, (dict, list)):
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def _add_file(db, path: Path, category: str, max_bytes: int) -> None:
    if db.execute("SELECT 1 FROM handoff_files WHERE source_path=?", (str(path),)).fetchone():
        return
    status, digest = "stored", None
    lowered = {part.casefold() for part in path.parts}
    if (
        lowered & SECRET_PARTS
        or path.name.casefold().startswith(".env")
        or path.name.casefold().endswith("storage-state.json")
        or path.suffix.lower() not in ASSET_SUFFIXES
        and path.name != ".gitignore"
        and not (
            category == "workspace_change_snapshot" and path.name.startswith("user_profile.yaml.")
        )
    ):
        status = "excluded_file_type_or_secret_path"
    elif not path.is_file():
        status = "missing"
    elif path.stat().st_size > max_bytes:
        status = "over_size_limit"
    else:
        before = path.stat()
        digest = _fingerprint(path)
        existing = db.execute("SELECT rowid FROM handoff_assets WHERE sha256=?", (digest,))
        if not existing.fetchone():
            cursor = db.execute(
                "INSERT INTO handoff_assets(sha256,size_bytes,content) VALUES (?,?,zeroblob(?))",
                (digest, before.st_size, before.st_size),
            )
            copied = hashlib.sha256()
            with db.blobopen("handoff_assets", "content", cursor.lastrowid) as blob:
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        blob.write(chunk)
                        copied.update(chunk)
            if copied.hexdigest() != digest:
                raise ValueError("A source changed while copying; retry into a new directory")
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("A source changed while copying; retry into a new directory")
    db.execute("INSERT INTO handoff_files VALUES (?,?,?,?)", (str(path), category, digest, status))


def _schema(db) -> None:
    db.executescript("""
        CREATE TABLE handoff_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE handoff_assets (
            sha256 TEXT UNIQUE NOT NULL, size_bytes INTEGER NOT NULL, content BLOB NOT NULL);
        CREATE TABLE handoff_files (
            source_path TEXT PRIMARY KEY, category TEXT NOT NULL,
            sha256 TEXT, status TEXT NOT NULL);
        CREATE TABLE handoff_source_checks (
            source_path TEXT NOT NULL, expected_sha256 TEXT NOT NULL,
            actual_sha256 TEXT, matches INTEGER NOT NULL);
        CREATE TABLE handoff_applications (
            job_id TEXT PRIMARY KEY, company TEXT, company_key TEXT, title TEXT,
            canonical_url TEXT, status TEXT, cv_source_path TEXT, cv_expected_sha256 TEXT,
            cv_actual_sha256 TEXT, cv_matches INTEGER, receipt_source_path TEXT);
        CREATE VIEW handoff_companies AS
            SELECT company_key, MIN(company) AS example_label, COUNT(*) AS job_count,
                SUM(status='submitted') AS submitted_records
            FROM handoff_applications WHERE company_key != '' GROUP BY company_key;
        CREATE INDEX handoff_application_company ON handoff_applications(company_key);
        CREATE VIEW handoff_steps AS SELECT sequence, run_id, job_id, event_type,
            timestamp, payload FROM browser_batch_activity;
    """)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], encoding="utf-8")


def _receipt_gaps(db, root: Path, jobs: list) -> int:
    references: dict[str, list[str]] = {}
    for job_id, _, status, payload in jobs:
        if status == "submitted":
            references[job_id] = list(_references(json.loads(payload).get("latest_evidence")))
    for job_id, payload in db.execute(
        "SELECT job_id,payload FROM browser_batch_activity WHERE event_type='confirmation'"
    ):
        if job_id in references:
            references[job_id].extend(_references(json.loads(payload)))
    stored = {
        row[0] for row in db.execute("SELECT source_path FROM handoff_files WHERE status='stored'")
    }
    return sum(
        not any(str(_path(root, raw)) in stored for raw in paths) for paths in references.values()
    )


def export_handoff(
    *,
    root: Path,
    output: Path,
    cv_directory: Path | None = None,
    include_workspace_changes: bool = False,
    max_asset_bytes: int = 300_000_000,
) -> dict:
    root = root.resolve(strict=True)
    runtime = (root / "runtime-data").resolve(strict=True)
    output = output.resolve()
    if output == runtime or not output.is_relative_to(runtime) or output.exists():
        raise ValueError("Output must be a new child of the source runtime-data directory")
    if max_asset_bytes < 1:
        raise ValueError("max_asset_bytes must be positive")
    state_path = runtime / "browser-batch-state.json"
    state = _json(state_path) if state_path.exists() else {}
    if cv_directory is None:
        cv_directory = _path(root, state["cv_directory"])
    cv_directory = cv_directory.resolve(strict=True)
    if not cv_directory.is_dir():
        raise ValueError("CV directory must be a directory")
    if output.is_relative_to(cv_directory) or cv_directory.is_relative_to(output):
        raise ValueError("Output and CV directory must not overlap")
    ledger = runtime / "browser-batch.sqlite3"
    if not ledger.is_file():
        raise ValueError("The authoritative browser ledger is missing")
    started = datetime.now(UTC).isoformat()
    output.mkdir(parents=True, exist_ok=False)
    database = output / "application-knowledge.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(database)) as db:
            # SQLite backup includes committed WAL contents and does not write the live ledger.
            source.backup(db)
            db.execute("PRAGMA trusted_schema=OFF")
            _schema(db)
            refs: dict[Path, str] = {}
            for relative, category in SOURCE_FILES.items():
                path = root / relative
                if path.exists():
                    refs[path.resolve()] = category
            for path in sorted(cv_directory.rglob("*")):
                if path.is_file() and path.suffix.lower() == ".pdf":
                    resolved = path.resolve()
                    if not resolved.is_relative_to(cv_directory):
                        raise ValueError("CV link points outside the supplied directory")
                    refs[resolved] = "cv_catalog"
            checks = []
            cache_path = runtime / "verified-answer-cache.json"
            if cache_path.exists():
                for item in _json(cache_path).get("source_files", []):
                    path = _path(root, item["path"])
                    refs.setdefault(path, "fact_provenance")
                    checks.append((path, item["sha256"]))
            jobs = list(
                db.execute(
                    "SELECT job_id,canonical_url,status,payload "
                    "FROM browser_batch_jobs ORDER BY job_id"
                )
            )
            for _, _, _, payload in jobs:
                job = json.loads(payload)
                cv = job.get("selected_cv") or {}
                if cv.get("path"):
                    refs.setdefault(_path(root, cv["path"]), "historical_cv")
                for raw in _references(job):
                    refs.setdefault(_path(root, raw), "receipt_or_activity_evidence")
            for table in ("browser_batch_events", "browser_batch_activity"):
                for (payload,) in db.execute(f"SELECT payload FROM {table}"):
                    for raw in _references(json.loads(payload)):
                        refs.setdefault(_path(root, raw), "receipt_or_activity_evidence")
            # Checkpoints are instructions/state only, never renewed authorization.
            for pattern in ("run-*/checkpoint.json", "run-*/continuation-checkpoint.json"):
                for path in runtime.glob(pattern):
                    refs.setdefault(path.resolve(), "checkpoint_snapshot")
            git_info = {}
            if include_workspace_changes:
                git_info = {
                    "head": _git(root, "rev-parse", "HEAD").strip(),
                    "branch": _git(root, "branch", "--show-current").strip(),
                }
                names = _git(
                    root, "ls-files", "--modified", "--others", "--exclude-standard", "-z"
                ).split("\0")
                for name in names:
                    if not name or Path(name).parts[0] in {"runtime-data", "outputs"}:
                        continue
                    path = _path(root, name)
                    if path.is_relative_to(root):
                        refs.setdefault(path, "workspace_change_snapshot")
            for path, category in sorted(refs.items()):
                _add_file(db, path, category, max_asset_bytes)
            for path, expected in checks:
                actual = db.execute(
                    "SELECT sha256 FROM handoff_files WHERE source_path=?", (str(path),)
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO handoff_source_checks VALUES (?,?,?,?)",
                    (str(path), expected, actual, int(actual == expected)),
                )
            for job_id, url, status, payload in jobs:
                job = json.loads(payload)
                company = job.get("company") or ""
                company_key = " ".join(unicodedata.normalize("NFKC", company).casefold().split())
                cv = job.get("selected_cv") or {}
                cv_path = str(_path(root, cv["path"])) if cv.get("path") else ""
                row = db.execute(
                    "SELECT sha256 FROM handoff_files WHERE source_path=?", (cv_path,)
                ).fetchone()
                actual = row[0] if row else None
                receipt = (job.get("latest_evidence") or {}).get("artifact", "")
                db.execute(
                    "INSERT INTO handoff_applications VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        company,
                        company_key,
                        job.get("title", ""),
                        url,
                        status,
                        cv_path,
                        cv.get("sha256"),
                        actual,
                        int(actual is not None and actual == cv.get("sha256")),
                        receipt,
                    ),
                )
            summary = {
                "schema_version": "private-application-handoff-v1",
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "source_workspace": str(root),
                "source_ledger": str(ledger),
                "cv_directory": str(cv_directory),
                "source_git": git_info,
                "database": database.name,
                "status_counts": dict(Counter(row[2] for row in jobs)),
                "company_label_groups": db.execute(
                    "SELECT COUNT(*) FROM handoff_companies"
                ).fetchone()[0],
                "activity_count": db.execute(
                    "SELECT COUNT(*) FROM browser_batch_activity"
                ).fetchone()[0],
                "file_status_counts": dict(
                    db.execute("SELECT status,COUNT(*) FROM handoff_files GROUP BY status")
                ),
                "file_category_counts": dict(
                    db.execute("SELECT category,COUNT(*) FROM handoff_files GROUP BY category")
                ),
                "stored_cv_files": db.execute(
                    "SELECT COUNT(*) FROM handoff_files "
                    "WHERE category IN ('cv_catalog','historical_cv') AND status='stored'"
                ).fetchone()[0],
                "source_hash_mismatches": db.execute(
                    "SELECT COUNT(*) FROM handoff_source_checks WHERE matches=0"
                ).fetchone()[0],
                "selected_cv_mismatches": db.execute(
                    "SELECT COUNT(*) FROM handoff_applications "
                    "WHERE cv_source_path != '' AND cv_matches=0"
                ).fetchone()[0],
                "submitted_without_stored_receipt_artifact": _receipt_gaps(db, root, jobs),
                "limits": [
                    "Private snapshot, not the live ledger or authorization to submit.",
                    "Ledger backup is consistent; files have individually verified copy hashes.",
                    "Source checks compare saved cache fingerprints, not factual truth.",
                    "Company label groups are not verified distinct employers.",
                    "Selection is not proof of upload; inspect original activity and receipts.",
                    "Absolute source paths are preserved; this is not an automatic restore.",
                    "Missing/excluded files remain explicit; missing proof cannot authorize reuse.",
                ],
            }
            db.execute(
                "INSERT INTO handoff_metadata VALUES (?,?)",
                ("manifest", json.dumps(summary, ensure_ascii=False)),
            )
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Exported database failed integrity_check")
    summary["database_sha256"] = _fingerprint(database)
    (output / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cv-dir", type=Path)
    parser.add_argument("--include-workspace-changes", action="store_true")
    parser.add_argument("--max-asset-bytes", type=int, default=300_000_000)
    args = parser.parse_args(argv)
    try:
        summary = export_handoff(
            root=args.source_root,
            output=args.output_dir,
            cv_directory=args.cv_dir,
            include_workspace_changes=args.include_workspace_changes,
            max_asset_bytes=args.max_asset_bytes,
        )
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        parser.exit(2, f"handoff export: {type(error).__name__}; source ledger unchanged\n")
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "schema_version",
                    "status_counts",
                    "activity_count",
                    "file_status_counts",
                    "source_hash_mismatches",
                    "selected_cv_mismatches",
                    "database_sha256",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
