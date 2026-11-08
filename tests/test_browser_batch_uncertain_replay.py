"""Replaying one saved uncertain observation must not block later confirmed outcomes."""

from __future__ import annotations

import sqlite3

import pytest

from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot

UNCERTAIN = "https://example.test/jobs/unconfirmed"
CONFIRMED = "https://example.test/jobs/confirmed"


def _context(tmp_path):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=UNCERTAIN), TabSnapshot(url=CONFIRMED)])
    evidence = ObservedEvidence(
        kind="submit_clicked",
        text="Clicked Submit once; no positive receipt was observed. No retry performed.",
        url=UNCERTAIN,
        artifact="observed-unconfirmed-submit.txt",
    )
    ledger.record(UNCERTAIN, BatchStatus.SUBMISSION_UNCERTAIN, evidence)
    return ledger, evidence


def test_same_uncertain_replay_is_noop_and_does_not_prevent_later_confirmation(tmp_path):
    ledger, evidence = _context(tmp_path)
    with sqlite3.connect(ledger.path) as connection:
        history_before = connection.execute("SELECT COUNT(*) FROM browser_batch_events").fetchone()[
            0
        ]
    replay = ObservedEvidence.model_validate(evidence.model_dump(exclude={"observed_at"}))
    assert (
        ledger.record(UNCERTAIN, BatchStatus.SUBMISSION_UNCERTAIN, replay).latest_evidence
        == evidence
    )
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM browser_batch_events").fetchone()[0] == (
            history_before
        )
    ledger.record(
        CONFIRMED,
        BatchStatus.SUBMITTED,
        ObservedEvidence(kind="page_confirmation", text="Application submitted!"),
    )
    assert ledger.export()["counts"]["submission_uncertain"] == 1
    assert ledger.export()["counts"]["submitted"] == 1
    assert ledger.get(UNCERTAIN).latest_evidence == evidence
    assert ledger.claim_next() is None


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Clicked Submit again; still no receipt"},
        {"url": "https://example.test/jobs/another"},
        {"artifact": "new-submit-attempt.txt"},
        {"reference": "a-different-attempt"},
    ],
)
def test_changed_uncertain_evidence_remains_blocked_from_replay_or_retry(tmp_path, change):
    ledger, evidence = _context(tmp_path)
    with pytest.raises(ValueError, match="Reconcile"):
        ledger.record(
            UNCERTAIN,
            BatchStatus.SUBMISSION_UNCERTAIN,
            evidence.model_copy(update=change),
        )
    assert ledger.get(UNCERTAIN).status == BatchStatus.SUBMISSION_UNCERTAIN
    assert ledger.get(UNCERTAIN).latest_evidence == evidence


@pytest.mark.parametrize("status", [BatchStatus.BLOCKED, BatchStatus.CLOSED, BatchStatus.SKIPPED])
def test_uncertain_replay_fix_does_not_allow_other_state_changes(tmp_path, status):
    ledger, evidence = _context(tmp_path)
    with pytest.raises(ValueError, match="Reconcile"):
        ledger.record(UNCERTAIN, status, evidence)
    assert ledger.get(UNCERTAIN).status == BatchStatus.SUBMISSION_UNCERTAIN
