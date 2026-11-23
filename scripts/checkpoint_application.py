"""Archive one recorded browser outcome, push ciphertext, and verify remote bytes.

No browser actions or application submissions occur here. Configuration, keys,
plaintext bundles, and verification downloads remain outside the public checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.encrypted_application_archive import (
    encrypt_file, inspect_archive, split_ciphertext, verify_zip,
)

TERMINAL = {"submitted", "already_applied", "submission_uncertain", "blocked", "closed", "skipped"}
SUFFIXES = {".json", ".txt", ".md", ".csv", ".jpg", ".jpeg", ".png", ".pdf", ".py"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    if result.returncode:
        # Do not echo command output which could contain private paths or auth errors.
        raise RuntimeError("Git operation failed: " + args[0])
    return result.stdout.strip()


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("source_root", "run_dir", "archive_worktree", "key_file", "private_dir"):
        cfg[key] = Path(cfg[key]).resolve()
    if not cfg["run_dir"].is_relative_to(cfg["source_root"] / "runtime-data"):
        raise ValueError("Run must be inside private runtime-data")
    if not cfg["private_dir"].is_relative_to(cfg["source_root"] / "runtime-data"):
        raise ValueError("Staging must be inside private runtime-data")
    if cfg["private_dir"].is_relative_to(cfg["run_dir"]):
        raise ValueError("Staging cannot be included in run artifacts")
    if cfg["key_file"].is_relative_to(cfg["source_root"]) or cfg["key_file"].is_relative_to(cfg["archive_worktree"]):
        raise ValueError("Key must be outside both checkouts")
    if cfg["archive_worktree"] == cfg["source_root"] or not cfg["branch"].startswith("codex/"):
        raise ValueError("Use an isolated checkpoint branch and checkout")
    return cfg


def read_job(connection: sqlite3.Connection, url: str) -> dict:
    hits = []
    for (payload,) in connection.execute("SELECT payload FROM browser_batch_jobs"):
        item = json.loads(payload)
        if url.rstrip("/") in {u.rstrip("/") for u in [item["canonical_url"], *item.get("source_urls", [])]}:
            hits.append(item)
    if len(hits) != 1:
        raise ValueError("Expected exactly one application")
    return hits[0]


def review_activity(job: dict, events: list[dict], *, historical: bool = False) -> list[str]:
    gaps = []
    kinds = {e["event_type"] for e in events}
    if job["status"] not in TERMINAL:
        gaps.append("outcome_not_terminal")
    if not events:
        gaps.append("no_step_records")
    if job["status"] == "submitted":
        for kind in ("observation", "form_field", "submit_attempt", "confirmation"):
            if kind not in kinds:
                gaps.append("missing_" + kind)
        reviews = [e for e in events if e.get("details", {}).get("reviewed_fields")]
        labels = {e.get("entered", {}).get("label") for e in events
                  if e["event_type"] == "form_field" and isinstance(e.get("entered"), dict)}
        if not reviews:
            gaps.append("missing_field_coverage_review")
        else:
            for label in reviews[-1]["details"]["reviewed_fields"]:
                if label not in labels:
                    gaps.append("unrecorded_reviewed_field:" + label)
        if job.get("selected_cv"):
            matches = [e for e in events if e["event_type"] == "upload"
                       and e.get("cv_sha256") == job["selected_cv"]["sha256"]]
            if not matches:
                gaps.append("missing_matching_cv_upload")
        ordered = [e["event_type"] for e in events]
        if "submit_attempt" in ordered and "confirmation" in ordered:
            if ordered.index("confirmation") < ordered.index("submit_attempt"):
                gaps.append("confirmation_before_submit")
        if not job.get("latest_evidence", {}).get("artifact"):
            gaps.append("missing_receipt_artifact")
    elif job["status"] == "submission_uncertain" and "submit_attempt" not in kinds:
        gaps.append("missing_submit_attempt")
    if gaps and not historical:
        raise ValueError("Activity coverage incomplete: " + ", ".join(gaps))
    return gaps


def private_state(cfg: dict) -> Path:
    return cfg["private_dir"] / "sync-state.json"


def guard(cfg: dict) -> dict:
    path = private_state(cfg)
    if not path.exists():
        raise ValueError("A verified initial checkpoint is required")
    state = json.loads(path.read_text())
    if state.get("pending"):
        raise ValueError("Previous encrypted checkpoint is not remotely verified")
    scope = json.loads((cfg["run_dir"] / "scope.json").read_text(encoding="utf-8"))
    if len(scope["outcomes"]) != state.get("outcome_count"):
        raise ValueError("New outcome is not remotely checkpointed")
    return {"allowed": True, "verified_commit": state["verified_commit"]}


def make_bundle(cfg: dict, folder: Path, url: str | None) -> tuple[Path, dict]:
    source = cfg["source_root"]
    live = source / "runtime-data/browser-batch.sqlite3"
    backup = folder / "browser-batch.sqlite3"
    with sqlite3.connect(live.as_uri() + "?mode=ro", uri=True) as incoming:
        with sqlite3.connect(backup) as outgoing:
            incoming.backup(outgoing)
    with sqlite3.connect(backup) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Ledger snapshot integrity failed")
        scope = json.loads((cfg["run_dir"] / "scope.json").read_text(encoding="utf-8"))
        urls = [url] if url else list(dict.fromkeys(o["url"] for o in scope["outcomes"]))
        applications = []
        extra = []
        for target in urls:
            job = read_job(connection, target)
            events = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM browser_batch_activity WHERE job_id=? ORDER BY sequence", (job["job_id"],))]
            gaps = review_activity(job, events, historical=url is None)
            applications.append({"application": job, "steps": events, "historical_gaps": gaps})
            cv = job.get("selected_cv")
            if cv:
                path = Path(cv["path"])
                if not path.is_file() or digest(path) != cv["sha256"]:
                    raise ValueError("Selected CV is missing or changed")
                extra.append(("cvs/" + cv["file"], path))
            for evidence in [job.get("latest_evidence") or {}, *events]:
                paths = evidence.get("artifact_paths", [])
                if evidence.get("artifact"):
                    paths.append(evidence["artifact"])
                for value in paths:
                    path = Path(value)
                    if not path.is_file() or path.is_symlink():
                        raise ValueError("Referenced evidence is missing or a symlink")
                    if path.suffix.lower() in SUFFIXES:
                        extra.append(("evidence/" + digest(path) + path.suffix.lower(), path))
    audit = folder / "applications.json"
    save(audit, {"captured_at": datetime.now(timezone.utc).isoformat(),
                 "run_id": cfg["run_id"], "historical_baseline": url is None,
                 "applications": applications,
                 "limits": "Only observed records are included; historical gaps are not reconstructed. Credentials are excluded."})
    files = [("browser-batch.sqlite3", backup), ("applications.json", audit), *extra]
    for path in cfg["run_dir"].rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in SUFFIXES:
            files.append(("run/" + path.relative_to(cfg["run_dir"]).as_posix(), path))
    for name in ("verified-profile-overlay.json", "verified-answer-cache.json", "employer-policy.json"):
        path = source / "runtime-data" / name
        if path.is_file():
            files.append(("profile/" + name, path))
    if url is None:
        for path in Path(scope["cv_directory"]).glob("*.pdf"):
            files.append(("cvs/" + path.name, path))
    unique = dict(files)
    archive = folder / "bundle.zip"
    manifest = {"schema_version": "jobapply-private-bundle-v1", "files": []}
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, path in unique.items():
            if path.resolve() == cfg["key_file"] or path.suffix.lower() in {".key", ".pem", ".env"}:
                raise ValueError("Key or credential file cannot enter archive")
            data = path.read_bytes()
            bundle.writestr(name, data)
            manifest["files"].append({"archive_path": name, "source_path": str(path),
                                      "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        bundle.writestr("bundle-manifest.json", json.dumps(manifest, ensure_ascii=False))
    verify_zip(archive)
    return archive, {"outcome_count": len(scope["outcomes"]), "application_count": len(applications),
                     "file_count": len(unique), "historical_gaps": sum(bool(x["historical_gaps"]) for x in applications)}


def verify_remote(cfg: dict, state: dict) -> dict:
    repo = cfg["archive_worktree"]
    commit = state["commit"]
    remote = git(repo, "ls-remote", "origin", "refs/heads/" + cfg["branch"])
    if remote.split()[0] != commit:
        raise ValueError("Remote branch does not match checkpoint commit")
    folder = Path(state["folder"])
    cache = folder / ("remote-lfs-" + uuid.uuid4().hex[:8])
    includes = ",".join(state["files"][:-1])
    git(repo, "-c", "lfs.storage=" + str(cache), "lfs", "fetch", "origin", commit,
        "--include=" + includes, "--exclude=")
    public = json.loads((repo / state["files"][-1]).read_text())
    download = folder / ("remote-copy-" + uuid.uuid4().hex[:8])
    download.mkdir()
    for part in public["parts"]:
        sha = part["sha256"]
        obj = cache / "objects" / sha[:2] / sha[2:4] / sha
        if not obj.is_file() or digest(obj) != sha:
            raise ValueError("Remote ciphertext verification failed")
        # Copy only independently downloaded bytes, not the publishing checkout.
        (download / part["file"]).write_bytes(obj.read_bytes())
    remote_manifest = git(repo, "show", commit + ":" + state["files"][-1])
    if json.loads(remote_manifest) != public:
        raise ValueError("Committed ciphertext manifest mismatch")
    manifest = download / "parts.json"
    save(manifest, public)
    result = inspect_archive(manifest, cfg["key_file"])
    return {"authenticated": result["authenticated"], "files_verified": result["files"],
            "downloaded_from_remote": True}


def checkpoint(cfg: dict, url: str | None) -> dict:
    repo = cfg["archive_worktree"]
    state_path = private_state(cfg)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if git(repo, "branch", "--show-current") != cfg["branch"]:
        raise ValueError("Wrong checkpoint branch")
    if git(repo, "remote", "get-url", "origin") != cfg["expected_origin"]:
        raise ValueError("Unexpected publication repository")
    if state.get("pending"):
        if state.get("job_url") != url:
            raise ValueError("Retry the pending checkpoint before another application")
        if not state.get("commit"):
            raise ValueError("Incomplete preparation needs inspection before retry")
    else:
        if git(repo, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("Checkpoint checkout must be clean")
        folder = cfg["private_dir"] / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        bundle, audit = make_bundle(cfg, folder, url)
        encrypted = folder / "bundle.enc"
        encrypt_file(bundle, encrypted, cfg["key_file"])
        inspect_archive(encrypted, cfg["key_file"])
        name = "checkpoint-" + folder.name.lower()
        public = split_ciphertext(encrypted, repo / "application-data", name, part_bytes=64 * 1024 * 1024)
        files = ["application-data/" + p["file"] for p in public["parts"]] + ["application-data/" + name + ".json"]
        state = {"pending": True, "job_url": url, "folder": str(folder), "files": files, **audit}
        save(state_path, state)
        git(repo, "add", "--", *files)
        if set(git(repo, "diff", "--cached", "--name-only").splitlines()) != set(files):
            raise ValueError("Unexpected staged paths; refusing publication")
        git(repo, "commit", "-m", "Preserve encrypted application checkpoint")
        state["commit"] = git(repo, "rev-parse", "HEAD")
        save(state_path, state)
    if git(repo, "rev-parse", "HEAD") != state["commit"]:
        raise ValueError("Pending checkpoint commit changed; inspect before publishing")
    git(repo, "push", "-u", "origin", cfg["branch"])
    verified = verify_remote(cfg, state)
    state.update(pending=False, verified_commit=state["commit"], verification=verified,
                 verified_at=datetime.now(timezone.utc).isoformat())
    save(state_path, state)
    receipt = Path(state["folder"]) / "publication-receipt.json"
    save(receipt, state)
    return {"verified_commit": state["commit"], "outcome_count": state["outcome_count"],
            "file_count": state["file_count"], "historical_gaps": state["historical_gaps"], **verified}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bootstrap", "checkpoint", "guard"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job-url")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.command == "checkpoint" and not args.job_url:
            raise ValueError("A job URL is required")
        result = guard(cfg) if args.command == "guard" else checkpoint(cfg, args.job_url if args.command == "checkpoint" else None)
        print(json.dumps(result))
        return 0
    except Exception as error:
        # No raw user answers, CV paths, key contents, or tool stderr in public logs.
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "reason": str(error) if isinstance(error, ValueError) else "Operation failed; checkpoint remains pending"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
