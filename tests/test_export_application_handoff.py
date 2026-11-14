"""Round-trip evidence and CV bytes without mutating or replacing the live ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from scripts.export_application_handoff import export_handoff


def _workspace(tmp_path):
    runtime = tmp_path / "runtime-data"
    runtime.mkdir()
    cvs = tmp_path / "cvs"
    cvs.mkdir()
    cv = cvs / "engineer.pdf"
    cv.write_bytes(b"synthetic resume bytes")
    (cvs / "unused.pdf").write_bytes(b"another synthetic CV")
    receipt = runtime / "receipt.png"
    receipt.write_bytes(b"synthetic receipt bytes")
    screenshot = runtime / "upload.png"
    screenshot.write_bytes(b"synthetic upload bytes")
    profile = tmp_path / "user_profile.yaml"
    profile.write_text("personal:\n  full_name: Example Applicant\n", encoding="utf-8")
    (runtime / "browser-batch-state.json").write_text(
        json.dumps({"cv_directory": str(cvs)}), encoding="utf-8"
    )
    cache = {
        "source_files": [
            {"path": str(profile), "sha256": hashlib.sha256(profile.read_bytes()).hexdigest()}
        ]
    }
    (runtime / "verified-answer-cache.json").write_text(json.dumps(cache), encoding="utf-8")
    database = runtime / "browser-batch.sqlite3"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executescript("""
        CREATE TABLE browser_batch_jobs(job_id TEXT PRIMARY KEY, canonical_url TEXT,
            status TEXT, payload TEXT);
        CREATE TABLE browser_batch_events(event_id INTEGER, job_id TEXT, payload TEXT);
        CREATE TABLE browser_batch_activity(sequence INTEGER, run_id TEXT, job_id TEXT,
            event_type TEXT, timestamp TEXT, payload TEXT);
    """)
    job = {
        "company": "Example Ltd",
        "title": "Engineer",
        "source_urls": ["https://example.test/alias/42"],
        "selected_cv": {"path": str(cv), "sha256": hashlib.sha256(cv.read_bytes()).hexdigest()},
        "latest_evidence": {"kind": "page_confirmation", "artifact": str(receipt)},
    }
    writer.execute(
        "INSERT INTO browser_batch_jobs VALUES (?,?,?,?)",
        ("job-42", "https://example.test/jobs/42", "submitted", json.dumps(job)),
    )
    writer.execute(
        "INSERT INTO browser_batch_activity VALUES (?,?,?,?,?,?)",
        (
            1,
            "run-a",
            "job-42",
            "cv_uploaded",
            "2026-11-14T00:00:00Z",
            json.dumps({"artifact_paths": [str(screenshot)]}),
        ),
    )
    writer.commit()
    return runtime, cvs, cv, profile, writer


def test_snapshot_contains_committed_wal_all_cvs_profile_and_steps(tmp_path):
    runtime, _, cv, profile, writer = _workspace(tmp_path)
    try:
        before = list(writer.iterdump())
        summary = export_handoff(root=tmp_path, output=runtime / "handoff")
        assert list(writer.iterdump()) == before
        assert summary["status_counts"] == {"submitted": 1}
        assert summary["activity_count"] == 1
        assert summary["stored_cv_files"] == 2
        assert summary["source_hash_mismatches"] == summary["selected_cv_mismatches"] == 0
        assert summary["submitted_without_stored_receipt_artifact"] == 0
        with closing(sqlite3.connect(runtime / "handoff/application-knowledge.sqlite3")) as db:
            for source in (cv, profile, runtime / "receipt.png", runtime / "upload.png"):
                stored = db.execute(
                    "SELECT a.content FROM handoff_files f "
                    "JOIN handoff_assets a USING(sha256) WHERE f.source_path=?",
                    (str(source),),
                ).fetchone()[0]
                assert stored == source.read_bytes()
            assert db.execute("SELECT event_type FROM handoff_steps").fetchone() == ("cv_uploaded",)
            job = json.loads(db.execute("SELECT payload FROM browser_batch_jobs").fetchone()[0])
            assert job["source_urls"] == ["https://example.test/alias/42"]
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()


def test_changed_cv_and_source_are_preserved_but_not_marked_verified(tmp_path):
    runtime, _, cv, profile, writer = _workspace(tmp_path)
    writer.close()
    cv.write_bytes(b"changed CV")
    profile.write_text("changed profile", encoding="utf-8")
    summary = export_handoff(root=tmp_path, output=runtime / "handoff")
    assert summary["source_hash_mismatches"] == 1
    assert summary["selected_cv_mismatches"] == 1
    with closing(sqlite3.connect(runtime / "handoff/application-knowledge.sqlite3")) as db:
        expected, actual, matches = db.execute(
            "SELECT cv_expected_sha256, cv_actual_sha256, cv_matches FROM handoff_applications"
        ).fetchone()
        assert actual == hashlib.sha256(cv.read_bytes()).hexdigest()
        assert expected != actual and matches == 0


def test_missing_evidence_and_secret_paths_remain_explicit(tmp_path):
    runtime, _, _, _, writer = _workspace(tmp_path)
    writer.close()
    (runtime / "receipt.png").unlink()
    secret = tmp_path / ".env"
    secret.write_text("synthetic-secret", encoding="utf-8")
    cache = runtime / "verified-answer-cache.json"
    cache.write_text(
        json.dumps({"source_files": [{"path": str(secret), "sha256": "0" * 64}]}), encoding="utf-8"
    )
    summary = export_handoff(root=tmp_path, output=runtime / "handoff")
    assert summary["file_status_counts"]["missing"] == 1
    assert summary["file_status_counts"]["excluded_file_type_or_secret_path"] == 1
    assert summary["submitted_without_stored_receipt_artifact"] == 1
    with closing(sqlite3.connect(runtime / "handoff/application-knowledge.sqlite3")) as db:
        assert db.execute(
            "SELECT sha256 FROM handoff_files WHERE source_path=?", (str(secret),)
        ).fetchone() == (None,)


def test_size_limit_records_unavailable_cv_instead_of_claiming_complete(tmp_path):
    runtime, _, _, _, writer = _workspace(tmp_path)
    writer.close()
    summary = export_handoff(root=tmp_path, output=runtime / "handoff", max_asset_bytes=1)
    assert summary["file_status_counts"]["over_size_limit"] > 0
    assert summary["selected_cv_mismatches"] == 1
    assert summary["stored_cv_files"] == 0


@pytest.mark.parametrize("relative", ["public", "runtime-data", "runtime-data/../public"])
def test_output_must_be_new_and_private(tmp_path, relative):
    runtime, _, _, _, writer = _workspace(tmp_path)
    writer.close()
    with pytest.raises(ValueError):
        export_handoff(root=tmp_path, output=tmp_path / relative)
    assert not (runtime / "application-knowledge.sqlite3").exists()


def test_rerun_never_overwrites_a_snapshot(tmp_path):
    runtime, _, _, _, writer = _workspace(tmp_path)
    writer.close()
    output = runtime / "handoff"
    export_handoff(root=tmp_path, output=output)
    before = (output / "manifest.json").read_bytes()
    with pytest.raises(ValueError):
        export_handoff(root=tmp_path, output=output)
    assert (output / "manifest.json").read_bytes() == before
