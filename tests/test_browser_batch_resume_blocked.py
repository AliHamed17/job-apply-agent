"""Only explicit, documented blocker recovery may re-enter the claim workflow."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from batch_apply.catalog import choose_cv_file
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from scripts.plan_browser_batch import main

URL = "https://example.test/jobs/blocked-role"
RESOLUTION = "Current user confirmed the required information that was previously unknown."


@pytest.fixture
def context(tmp_path):
    directory = tmp_path / "cvs"
    directory.mkdir()
    filename = "QA_Automation_Engineer_CV.pdf"
    (directory / filename).write_bytes(b"%PDF-1.4\nfixture")
    choice = choose_cv_file(directory, filename, source="test", reason="Reviewed role")
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="QA Engineer")])
    return ledger, choice, directory, filename


def _block(ledger):
    evidence = ObservedEvidence(kind="blocked_notice", text="Required mailing consent is missing")
    ledger.record(URL, BatchStatus.BLOCKED, evidence)
    return evidence


def test_explicit_resume_preserves_blocker_history_and_requires_claim(context):
    ledger, choice, _, _ = context
    original = _block(ledger)
    resumed = ledger.resume_blocked(URL, choice, RESOLUTION)
    assert resumed.status == BatchStatus.PREPARED
    assert resumed.selected_cv == choice
    assert resumed.latest_evidence.kind == "blocker_resume_note"
    assert resumed.latest_evidence.text == RESOLUTION
    with sqlite3.connect(ledger.path) as connection:
        history = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM browser_batch_events ORDER BY event_id"
            )
        ]
    assert history[-2]["evidence"]["text"] == original.text
    assert history[-1]["event"] == "blocker_resumed"
    assert history[-1]["reason"] == RESOLUTION
    assert ledger.claim_next().canonical_url == URL
    assert ledger.get(URL).status == BatchStatus.IN_PROGRESS


@pytest.mark.parametrize("resolution", ["", "  \n\t "])
def test_empty_resolution_does_not_change_blocker(context, resolution):
    ledger, choice, _, _ = context
    _block(ledger)
    before = ledger.get(URL)
    with pytest.raises(ValueError, match="nonempty"):
        ledger.resume_blocked(URL, choice, resolution)
    assert ledger.get(URL) == before


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_resume_requires_a_verified_cv(context, damage):
    ledger, choice, _, _ = context
    _block(ledger)
    before = ledger.get(URL)
    from pathlib import Path

    if damage == "missing":
        Path(choice.path).unlink()
    else:
        Path(choice.path).write_bytes(b"different PDF")
    with pytest.raises(ValueError, match="missing or changed"):
        ledger.resume_blocked(URL, choice, RESOLUTION)
    assert ledger.get(URL) == before


@pytest.mark.parametrize(
    "status",
    [
        BatchStatus.DISCOVERED,
        BatchStatus.PREPARED,
        BatchStatus.IN_PROGRESS,
        BatchStatus.SUBMISSION_UNCERTAIN,
        BatchStatus.SUBMITTED,
        BatchStatus.ALREADY_APPLIED,
        BatchStatus.CLOSED,
        BatchStatus.SKIPPED,
    ],
)
def test_resume_rejects_every_nonblocked_state(context, status):
    ledger, choice, _, _ = context
    if status in {BatchStatus.PREPARED, BatchStatus.IN_PROGRESS}:
        ledger.prepare(URL, choice, "Reviewed CV")
        if status == BatchStatus.IN_PROGRESS:
            ledger.claim_next()
    elif status != BatchStatus.DISCOVERED:
        kind = {
            BatchStatus.SUBMISSION_UNCERTAIN: "submit_clicked",
            BatchStatus.SUBMITTED: "page_confirmation",
            BatchStatus.ALREADY_APPLIED: "already_applied_notice",
        }.get(status, "closed_notice")
        ledger.record(URL, status, ObservedEvidence(kind=kind, text="Application submitted"))
    before = ledger.get(URL)
    with pytest.raises(ValueError, match="Only a blocked"):
        ledger.resume_blocked(URL, choice, RESOLUTION)
    assert ledger.get(URL) == before


def test_two_recovery_callers_cannot_resume_same_job_twice(context):
    ledger, choice, _, _ = context
    _block(ledger)

    def resume():
        try:
            return ledger.resume_blocked(URL, choice, RESOLUTION).status
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: resume(), range(2)))
    assert results.count(BatchStatus.PREPARED) == 1
    assert results.count(None) == 1


def test_cli_explicit_resume_builds_exact_catalog_choice(context, capsys):
    ledger, _, directory, filename = context
    _block(ledger)
    assert (
        main(
            [
                "resume-blocked",
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
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "prepared"
    assert output["selected_cv"]["file"] == filename
    assert output["selected_cv"]["source"] == "explicit_blocker_resume"
    assert ledger.claim_next().status == BatchStatus.IN_PROGRESS


def test_cli_does_not_accept_blank_resolution(context, capsys):
    ledger, _, directory, filename = context
    _block(ledger)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "resume-blocked",
                "--ledger",
                str(ledger.path),
                "--url",
                URL,
                "--cv-dir",
                str(directory),
                "--file",
                filename,
                "--resolution",
                "  ",
            ]
        )
    assert error.value.code == 2
    assert ledger.get(URL).status == BatchStatus.BLOCKED
    capsys.readouterr()
