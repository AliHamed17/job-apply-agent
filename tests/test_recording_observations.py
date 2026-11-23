"""Recording reviews are audited without turning video frames into applications."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from batch_apply.catalog import choose_cv_file
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from batch_apply.workflow import ObservedApplication, prepare_packet

URL = "https://example.test/jobs/42"


@pytest.fixture
def review(tmp_path):
    recording = tmp_path / "recording.mp4"
    recording.write_bytes(b"fixture recording contents")
    manifest = {
        "reviewed": True,
        "recording_path": str(recording),
        "recording_sha256": hashlib.sha256(recording.read_bytes()).hexdigest(),
        "recorded_at": "2026-11-21T16:13:12+02:00",
        "observations": [
            {
                "id": "frame-001",
                "offset_seconds": 12.5,
                "job_url": URL,
                "company": "Example",
                "title": "Validation Engineer",
                "step": "submit_clicked",
                "description": "Submit button was clicked.",
            },
        ],
    }
    return manifest


def run_import(tmp_path, manifest):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.import_recording_observations",
            str(path),
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def get_events(tmp_path):
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        return db.execute(
            "SELECT event_type, timestamp, payload FROM browser_batch_activity"
        ).fetchall()


def test_reviewed_click_is_idempotently_saved_as_an_observation_not_an_application(
    tmp_path, review
):
    first = run_import(tmp_path, review)
    assert first.returncode == 0, first.stderr
    again = run_import(tmp_path, review)
    assert again.returncode == 0, again.stderr
    events = get_events(tmp_path)
    assert len(events) == 1
    event_type, timestamp, payload = events[0]
    event = json.loads(payload)
    assert event_type == "observation"
    assert timestamp == "2026-11-21T14:13:24.500000Z"
    assert event["job_url"] == URL
    assert event["details"]["recording_sha256"] == review["recording_sha256"]
    assert event["details"]["offset_seconds"] == 12.5
    assert event["details"]["step"] == "submit_clicked"
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='browser_batch_jobs'"
        ).fetchone()


def test_changed_review_cannot_rewrite_a_previously_imported_observation(tmp_path, review):
    assert run_import(tmp_path, review).returncode == 0
    review["observations"][0]["description"] = "A different observation"
    review["observations"].insert(
        0,
        {
            "id": "new-first",
            "offset_seconds": 2,
            "job_url": URL,
            "step": "job_opened",
            "description": "The job was opened.",
        },
    )
    second = run_import(tmp_path, review)
    assert second.returncode != 0
    assert len(get_events(tmp_path)) == 1
    assert "A different observation" not in get_events(tmp_path)[0][2]


def _job_and_cv(tmp_path, url=URL):
    ledger = BatchLedger(tmp_path / "ledger.sqlite3")
    ledger.import_tabs([TabSnapshot(url=url, company="Example", title="QA")])
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir(exist_ok=True)
    (cv_dir / "qa.pdf").write_bytes(b"%PDF-1.4 synthetic")
    cv = choose_cv_file(cv_dir, "qa.pdf", source="test", reason="test match")
    return ledger, cv


@pytest.mark.parametrize("step", ["submit_clicked", "receipt_visible", "prior_applied"])
def test_prior_recording_attempt_prevents_preparation_and_packet(tmp_path, review, step):
    review["observations"][0]["step"] = step
    assert run_import(tmp_path, review).returncode == 0
    ledger, cv = _job_and_cv(tmp_path)
    with pytest.raises(ValueError, match="reconcil"):
        ledger.prepare(URL, cv, "match")
    with pytest.raises(ValueError, match="reconcil"):
        ledger.select_cv(URL, cv)
    assert ledger.get(URL).status == BatchStatus.DISCOVERED
    packet = prepare_packet(
        ObservedApplication(job=TabSnapshot(url=URL, company="Example")),
        ledger_path=ledger.path,
        cache_path=tmp_path / "not-needed.json",
        cv_directory=tmp_path / "cvs",
        policy_path=tmp_path / "no-policy.json",
    )
    assert packet["next_action"] == "reconcile_existing_attempt"


def test_benign_recording_observation_does_not_block_new_work(tmp_path, review):
    review["observations"][0]["step"] = "cv_selected"
    assert run_import(tmp_path, review).returncode == 0
    ledger, cv = _job_and_cv(tmp_path)
    assert ledger.prepare(URL, cv, "match").status == BatchStatus.PREPARED
    assert ledger.claim_next().status == BatchStatus.IN_PROGRESS


def test_recording_attempt_after_preparation_is_not_claimed(tmp_path, review):
    ledger, cv = _job_and_cv(tmp_path)
    ledger.prepare(URL, cv, "match")
    assert run_import(tmp_path, review).returncode == 0
    assert ledger.claim_next() is None
    assert ledger.get(URL).status == BatchStatus.PREPARED


def test_plan_skips_recording_hold_and_prepares_other_jobs(tmp_path, review):
    assert run_import(tmp_path, review).returncode == 0
    inventory = tmp_path / "tabs.json"
    second = "https://example.test/jobs/43"
    inventory.write_text(
        json.dumps(
            {
                "tabs": [
                    {"url": URL, "title": "QA Engineer", "company": "Example"},
                    {"url": second, "title": "QA Engineer", "company": "Example"},
                ]
            }
        )
    )
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    (cv_dir / "QA_Engineer_CV.pdf").write_bytes(b"%PDF-1.4 synthetic")
    output = tmp_path / "plan.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.plan_browser_batch",
            "plan",
            str(inventory),
            "--cv-dir",
            str(cv_dir),
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["recording_reconciliation_holds"][0]["url"] == URL
    jobs = {job["canonical_url"]: job["status"] for job in plan["jobs"]}
    assert jobs[URL] == "discovered"
    assert jobs[second] == "prepared"


def test_plan_reports_a_job_prepared_before_recording_was_imported(tmp_path, review):
    ledger, cv = _job_and_cv(tmp_path)
    ledger.prepare(URL, cv, "match")
    assert run_import(tmp_path, review).returncode == 0
    inventory = tmp_path / "tabs.json"
    inventory.write_text(json.dumps({"tabs": [{"url": URL, "title": "QA Engineer"}]}))
    output = tmp_path / "plan.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.plan_browser_batch",
            "plan",
            str(inventory),
            "--cv-dir",
            str(tmp_path / "cvs"),
            "--ledger",
            str(ledger.path),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["recording_reconciliation_holds"][0]["url"] == URL
    assert ledger.get(URL).status == BatchStatus.PREPARED
    assert ledger.claim_next() is None


def test_recording_attempt_after_claim_stops_pre_submit(tmp_path, review):
    ledger, cv = _job_and_cv(tmp_path)
    ledger.prepare(URL, cv, "match")
    assert ledger.claim_next().status == BatchStatus.IN_PROGRESS
    assert run_import(tmp_path, review).returncode == 0
    with pytest.raises(ValueError, match="reconcil"):
        ledger.pre_submit_check(URL, observed_employer="Example")
    assert ledger.get(URL).status == BatchStatus.IN_PROGRESS


def test_source_alias_recording_blocks_primary_url(tmp_path, review):
    ledger, cv = _job_and_cv(tmp_path)
    alias = "https://other.example/jobs/other42"
    with sqlite3.connect(ledger.path) as db:
        row = db.execute("SELECT payload FROM browser_batch_jobs").fetchone()
        job = json.loads(row[0])
        job["source_urls"].append(alias)
        db.execute("UPDATE browser_batch_jobs SET payload=?", (json.dumps(job),))
    review["observations"][0]["job_url"] = alias
    assert run_import(tmp_path, review).returncode == 0
    with pytest.raises(ValueError, match="reconcil"):
        ledger.prepare(URL, cv, "match")
    packet = prepare_packet(
        ObservedApplication(job=TabSnapshot(url=URL, company="Example")),
        ledger_path=ledger.path,
        cache_path=tmp_path / "not-needed.json",
        cv_directory=tmp_path / "cvs",
        policy_path=tmp_path / "no-policy.json",
    )
    assert packet["next_action"] == "reconcile_existing_attempt"


def test_reviewed_attempt_cannot_be_resumed_through_a_blocker(tmp_path, review):
    ledger, cv = _job_and_cv(tmp_path)
    ledger.record(URL, BatchStatus.BLOCKED, ObservedEvidence(kind="blocked_notice", text="Blocked"))
    assert run_import(tmp_path, review).returncode == 0
    with pytest.raises(ValueError, match="reconcil"):
        ledger.resume_blocked(URL, cv, "retry")
    assert ledger.get(URL).status == BatchStatus.BLOCKED


def test_reviewed_attempt_cannot_be_reconsidered_from_a_skip(tmp_path, review):
    ledger, cv = _job_and_cv(tmp_path)
    ledger.record(URL, BatchStatus.SKIPPED, ObservedEvidence(kind="skipped_notice", text="Skip"))
    assert run_import(tmp_path, review).returncode == 0
    evidence_path = tmp_path / "new.txt"
    evidence_path.write_text("new state")
    evidence = ObservedEvidence(
        kind="new_observed_state", text="New state", artifact=str(evidence_path)
    )
    with pytest.raises(ValueError, match="reconcil"):
        ledger.reconsider_skipped(URL, cv, "retry", evidence)
    assert ledger.get(URL).status == BatchStatus.SKIPPED


def test_legacy_regional_linkedin_observation_guards_numeric_www_url(tmp_path, review):
    review["observations"][0]["job_url"] = (
        "https://il.linkedin.com/jobs/view/some-engineer-at-example-1234567890"
    )
    assert run_import(tmp_path, review).returncode == 0
    url = "https://www.linkedin.com/jobs/view/1234567890"
    ledger, cv = _job_and_cv(tmp_path, url)
    with pytest.raises(ValueError, match="reconcil"):
        ledger.prepare(url, cv, "match")


def test_legacy_regional_job_links_ats_observation_to_incoming_www_url(tmp_path, review):
    regional = "https://il.linkedin.com/jobs/view/some-engineer-at-example-1234567890"
    ats = "https://jobs.example.test/jobs/42"
    ledger, cv = _job_and_cv(tmp_path, regional)
    with sqlite3.connect(ledger.path) as db:
        row = db.execute("SELECT payload FROM browser_batch_jobs").fetchone()
        job = json.loads(row[0])
        job["source_urls"].append(ats)
        db.execute("UPDATE browser_batch_jobs SET payload=?", (json.dumps(job),))
    review["observations"][0]["job_url"] = ats
    assert run_import(tmp_path, review).returncode == 0
    url = "https://www.linkedin.com/jobs/view/1234567890"
    ledger.import_tabs([TabSnapshot(url=url, company="Example", title="QA")])
    with pytest.raises(ValueError, match="reconcil"):
        ledger.prepare(url, cv, "match")
    packet = prepare_packet(
        ObservedApplication(job=TabSnapshot(url=url, company="Example")),
        ledger_path=ledger.path,
        cache_path=tmp_path / "not-needed.json",
        cv_directory=tmp_path / "cvs",
        policy_path=tmp_path / "no-policy.json",
    )
    assert packet["next_action"] == "reconcile_existing_attempt"


@pytest.mark.parametrize("hostname", ["evil-linkedin.com", "linkedin.com.evil.test"])
def test_recording_guard_does_not_treat_spoofed_linkedin_domain_as_alias(
    tmp_path, review, hostname
):
    review["observations"][0]["job_url"] = f"https://{hostname}/jobs/view/1234567890"
    assert run_import(tmp_path, review).returncode == 0
    url = "https://www.linkedin.com/jobs/view/1234567890"
    ledger, cv = _job_and_cv(tmp_path, url)
    assert ledger.prepare(url, cv, "match").status == BatchStatus.PREPARED


@pytest.mark.parametrize(
    "change",
    ["wrong_hash", "negative_offset", "secret", "not_reviewed", "numeric_reviewed", "huge_offset"],
)
def test_invalid_review_batch_is_rejected_without_storing_any_event(tmp_path, review, change):
    review["observations"].append(
        {
            "id": "frame-002",
            "offset_seconds": 20,
            "job_url": URL,
            "step": "form_filled",
            "description": "Name was entered.",
        }
    )
    if change == "wrong_hash":
        review["recording_sha256"] = "0" * 64
    elif change == "negative_offset":
        review["observations"][1]["offset_seconds"] = -1
    elif change == "secret":
        review["observations"][1]["description"] = "password=do-not-store"
    elif change == "not_reviewed":
        review["reviewed"] = False
    elif change == "numeric_reviewed":
        review["reviewed"] = 1
    else:
        review["observations"][1]["offset_seconds"] = 1e300
    result = run_import(tmp_path, review)
    assert result.returncode != 0
    assert "do-not-store" not in result.stderr
    assert "Traceback" not in result.stderr
    database = tmp_path / "ledger.sqlite3"
    if database.exists():
        assert b"do-not-store" not in database.read_bytes()
        with sqlite3.connect(database) as db:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE name='browser_batch_activity'"
            ).fetchone()
            assert (
                not exists
                or db.execute("SELECT count(*) FROM browser_batch_activity").fetchone()[0] == 0
            )
