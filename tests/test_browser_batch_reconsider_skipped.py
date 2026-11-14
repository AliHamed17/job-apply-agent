"""New facts may recover a skip, never erase or retry a submission history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from batch_apply.activity import ActivityStore
from batch_apply.catalog import choose_cv_file
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from scripts.plan_browser_batch import main

URL = "https://example.test/jobs/data-analyst"
RESOLUTION = "New recorded SQL duration supports the required 1-3 year band."


@pytest.fixture
def context(tmp_path):
    directory = tmp_path / "cvs"
    directory.mkdir()
    filename = "Data_Analyst_CV.pdf"
    (directory / filename).write_bytes(b"%PDF-1.4\noriginal CV")
    choice = choose_cv_file(directory, filename, source="test", reason="Reviewed CV")
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Data Analyst")])
    ledger.prepare(URL, choice, "Reviewed CV")
    old_artifact = tmp_path / "old-form.txt"
    old_artifact.write_text("Required SQL duration was unknown.", encoding="utf-8")
    ledger.record(
        URL,
        BatchStatus.SKIPPED,
        ObservedEvidence(
            kind="blocked_notice", text="SQL duration unknown", artifact=str(old_artifact)
        ),
    )
    artifact = tmp_path / "new-fact.json"
    artifact.write_text('{"SQL_years":2,"source":"original recording"}', encoding="utf-8")
    evidence = ObservedEvidence(
        kind="new_factual_evidence",
        text="Recorded active entry of 2 SQL years.",
        artifact=str(artifact),
    )
    ActivityStore(ledger.path)
    return ledger, choice, evidence, directory, filename


def history(ledger):
    with sqlite3.connect(ledger.path) as connection:
        return connection.execute(
            "SELECT event_id, status, payload FROM browser_batch_events ORDER BY event_id"
        ).fetchall()


def test_reconsider_preserves_history_and_requires_normal_claim(context):
    ledger, choice, evidence, _, _ = context
    before = history(ledger)
    prior = ledger.get(URL)
    recovered = ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert recovered.status == BatchStatus.PREPARED
    assert recovered.selected_cv == choice
    assert recovered.latest_evidence == evidence
    after = history(ledger)
    assert after[:-1] == before
    event = json.loads(after[-1][2])
    assert event["event"] == "skipped_reconsidered"
    assert event["reason"] == RESOLUTION
    assert event["details"]["previous_selected_cv"] == prior.selected_cv.model_dump()
    assert (
        event["details"]["evidence_artifact_sha256"]
        == hashlib.sha256(Path(evidence.artifact).read_bytes()).hexdigest()
    )
    assert ledger.claim_next().status == BatchStatus.IN_PROGRESS


@pytest.mark.parametrize("status", [s for s in BatchStatus if s != BatchStatus.SKIPPED])
def test_only_current_skipped_state_can_be_reconsidered(context, status):
    ledger, choice, evidence, _, _ = context
    # Emulate legacy states without invoking the recovery operation under test.
    with sqlite3.connect(ledger.path) as connection:
        job = ledger.get(URL).model_copy(update={"status": status})
        connection.execute(
            "UPDATE browser_batch_jobs SET status=?, payload=?",
            (
                status.value,
                job.model_dump_json(),
            ),
        )
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="Only a skipped"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


@pytest.mark.parametrize("event_type", ["submit_attempt", "confirmation"])
def test_prior_browser_submission_activity_blocks_reconsideration(context, event_type):
    ledger, choice, evidence, _, _ = context
    ActivityStore(ledger.path).append_batch(
        {
            "run_id": "older-run",
            "events": [
                {
                    "job_url": URL,
                    "event_type": event_type,
                    "observed": "A prior final application action was observed.",
                }
            ],
        }
    )
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="prior submission"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


def test_submit_under_a_known_source_url_alias_also_blocks_reconsideration(context):
    ledger, choice, evidence, _, _ = context
    alias = "https://another.example.test/jobs/provider-role"
    with sqlite3.connect(ledger.path) as connection:
        job = ledger.get(URL)
        job.source_urls.append(alias)
        connection.execute("UPDATE browser_batch_jobs SET payload=?", (job.model_dump_json(),))
    ActivityStore(ledger.path).append_batch(
        {
            "run_id": "older-alias-run",
            "events": [
                {
                    "job_url": alias,
                    "event_type": "submit_attempt",
                    "observed": "Clicked Submit",
                }
            ],
        }
    )
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="prior submission"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


def test_old_activity_artifact_does_not_become_new_evidence_with_a_new_timestamp(context):
    ledger, choice, evidence, _, _ = context
    old_time = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    ActivityStore(ledger.path).append_batch(
        {
            "run_id": "earlier-review",
            "events": [
                {
                    "job_url": URL,
                    "event_type": "observation",
                    "timestamp": old_time,
                    "artifact_paths": [evidence.artifact],
                    "observed": "Old form observation",
                }
            ],
        }
    )
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="new evidence artifact"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


def test_fresh_ordinary_state_observation_can_resolve_a_never_submitted_skip(context):
    ledger, choice, evidence, _, _ = context
    evidence = evidence.model_copy(update={"kind": "new_observed_state"})
    ActivityStore(ledger.path).append_batch(
        {
            "run_id": "new-state-review",
            "events": [
                {
                    "job_url": URL,
                    "event_type": "observation",
                    "artifact_paths": [evidence.artifact],
                    "observed": "Existing sign-in works now",
                }
            ],
        }
    )
    recovered = ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert recovered.status == BatchStatus.PREPARED


def test_reconsideration_does_not_replace_the_original_cv_with_another_valid_cv(context):
    ledger, _, evidence, directory, _ = context
    other = directory / "Python_Software_Engineer_CV.pdf"
    other.write_bytes(b"%PDF-1.4\nanother reviewed CV")
    choice = choose_cv_file(directory, other.name, source="test", reason="Different CV")
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="retain the original selected CV"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


def test_missing_browser_audit_history_fails_closed(context):
    ledger, choice, evidence, _, _ = context
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DROP TABLE browser_batch_activity")
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="activity history is required"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


@pytest.mark.parametrize(
    "status,kind",
    [
        (BatchStatus.SUBMITTED, "page_confirmation"),
        (BatchStatus.ALREADY_APPLIED, "already_applied_notice"),
        (BatchStatus.SUBMISSION_UNCERTAIN, "submit_clicked"),
        (BatchStatus.CLOSED, "closed_notice"),
        (BatchStatus.BLOCKED, "submit_clicked"),
    ],
)
def test_old_terminal_or_submit_evidence_cannot_be_hidden_by_current_skip(context, status, kind):
    ledger, choice, evidence, _, _ = context
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "INSERT INTO browser_batch_events(job_id,created_at,status,payload) VALUES(?,?,?,?)",
            (
                ledger.get(URL).job_id,
                "2026-01-01T00:00:00+00:00",
                status.value,
                json.dumps({"event": "observed_outcome", "evidence": {"kind": kind}}),
            ),
        )
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError, match="prior submission|closed history"):
        ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence)
    assert (ledger.get(URL), history(ledger)) == before


@pytest.mark.parametrize(
    "damage",
    [
        "blank_resolution",
        "wrong_kind",
        "blank_text",
        "missing_artifact",
        "empty_artifact",
        "reused_artifact",
        "copied_old_artifact",
        "stale_observation",
        "naive_observation",
        "missing_cv",
        "changed_cv",
    ],
)
def test_missing_or_stale_new_proof_and_cv_never_requeue(context, damage):
    ledger, choice, evidence, _, _ = context
    resolution = RESOLUTION
    if damage == "blank_resolution":
        resolution = " \n\t"
    elif damage == "wrong_kind":
        evidence = evidence.model_copy(update={"kind": "blocked_notice"})
    elif damage == "blank_text":
        evidence = evidence.model_copy(update={"text": " "})
    elif damage == "missing_artifact":
        Path(evidence.artifact).unlink()
    elif damage == "empty_artifact":
        Path(evidence.artifact).write_bytes(b"")
    elif damage in {"reused_artifact", "copied_old_artifact"}:
        original = Path(ledger.get(URL).latest_evidence.artifact)
        if damage == "reused_artifact":
            evidence = evidence.model_copy(update={"artifact": str(original)})
        else:
            Path(evidence.artifact).write_bytes(original.read_bytes())
    elif damage == "stale_observation":
        evidence = evidence.model_copy(
            update={
                "observed_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        )
    elif damage == "naive_observation":
        evidence = evidence.model_copy(update={"observed_at": "2026-01-01T00:00:00"})
    elif damage == "missing_cv":
        Path(choice.path).unlink()
    elif damage == "changed_cv":
        Path(choice.path).write_bytes(b"changed CV")
    before = ledger.get(URL), history(ledger)
    with pytest.raises(ValueError):
        ledger.reconsider_skipped(URL, choice, resolution, evidence)
    assert (ledger.get(URL), history(ledger)) == before


def test_concurrent_reconsideration_only_recovers_once(context):
    ledger, choice, evidence, _, _ = context

    def recover():
        try:
            return ledger.reconsider_skipped(URL, choice, RESOLUTION, evidence).status
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: recover(), range(2)))
    assert results.count(BatchStatus.PREPARED) == 1
    assert results.count(None) == 1


def test_cli_reconsideration_requires_and_records_exact_new_evidence(context, tmp_path, capsys):
    ledger, _, evidence, directory, filename = context
    evidence_json = tmp_path / "recovery-evidence.json"
    evidence_json.write_text(evidence.model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "reconsider-skipped",
                "--ledger",
                str(ledger.path),
                "--url",
                URL,
                "--cv-dir",
                str(directory),
                "--file",
                filename,
                "--resolution",
                RESOLUTION,
                "--evidence-json",
                str(evidence_json),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "prepared"
    assert output["selected_cv"]["file"] == filename
    assert output["selected_cv"]["source"] == "explicit_skip_reconsideration"
    assert output["latest_evidence"]["artifact"] == evidence.artifact


def test_cli_missing_evidence_does_not_mutate(context, capsys):
    ledger, _, _, directory, filename = context
    before = ledger.get(URL), history(ledger)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "reconsider-skipped",
                "--ledger",
                str(ledger.path),
                "--url",
                URL,
                "--cv-dir",
                str(directory),
                "--file",
                filename,
                "--resolution",
                RESOLUTION,
            ]
        )
    assert error.value.code == 2
    assert (ledger.get(URL), history(ledger)) == before
    capsys.readouterr()
