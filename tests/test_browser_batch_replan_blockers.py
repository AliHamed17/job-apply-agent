"""Accumulated CV decisions cannot clear an observed CAPTCHA/auth/form blocker."""

from __future__ import annotations

import json

import pytest

from batch_apply.catalog import choose_cv_file
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from scripts.plan_browser_batch import main

MATRIX = "https://campaign.adamtotal.co.il/matrix/jobs/123"
EMBEDDED = "https://example.com/jobs/embedded-456"


@pytest.fixture
def catalog(tmp_path):
    directory = tmp_path / "cvs"
    directory.mkdir()
    name = "Embedded_Software_Engineer_CV.pdf"
    (directory / name).write_bytes(b"%PDF-1.4\nfixture")
    return directory, name


def _choice(catalog):
    directory, name = catalog
    return choose_cv_file(directory, name, source="llm_browser_review", reason="Reviewed role")


@pytest.mark.parametrize(
    "reason",
    [
        "CAPTCHA detected on the application page",
        "Authentication requires the user",
        "Required work-authorization answer is unsupported",
    ],
)
def test_accumulated_selections_preserve_observed_blocker_and_claim_new_job(
    tmp_path,
    catalog,
    capsys,
    reason,
):
    directory, name = catalog
    database = tmp_path / "batch.sqlite3"
    ledger = BatchLedger(database)
    ledger.import_tabs([TabSnapshot(url=MATRIX, title="Matrix role")])
    ledger.select_cv(MATRIX, _choice(catalog))
    assert ledger.claim_next().canonical_url == MATRIX
    evidence = ObservedEvidence(kind="blocked_notice", text=reason, url=MATRIX)
    ledger.record(MATRIX, BatchStatus.BLOCKED, evidence)
    original = ledger.get(MATRIX)
    snapshot = tmp_path / "all-tabs.json"
    snapshot.write_text(
        json.dumps(
            {
                "tabs": [
                    {"url": MATRIX, "title": "Matrix role"},
                    {"url": EMBEDDED, "title": "Embedded Software Engineer"},
                ]
            }
        )
    )
    selections = tmp_path / "accumulated-selections.json"
    selections.write_text(
        json.dumps(
            [
                {"url": url, "cv_file": name, "reason": "Previously recorded LLM CV selection"}
                for url in (MATRIX, EMBEDDED)
            ]
        )
    )
    assert (
        main(
            [
                "plan",
                str(snapshot),
                "--ledger",
                str(database),
                "--cv-dir",
                str(directory),
                "--selections",
                str(selections),
            ]
        )
        == 0
    )
    preserved = ledger.get(MATRIX)
    assert preserved.status == BatchStatus.BLOCKED
    assert preserved.reason == reason
    assert preserved.latest_evidence == evidence
    assert preserved.selected_cv == original.selected_cv
    assert ledger.claim_next().canonical_url == EMBEDDED
    assert ledger.get(MATRIX).status == BatchStatus.BLOCKED
    capsys.readouterr()


def test_direct_cv_selection_cannot_resolve_captcha_blocker(tmp_path, catalog):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=MATRIX)])
    evidence = ObservedEvidence(kind="blocked_notice", text="CAPTCHA must be completed")
    ledger.record(MATRIX, BatchStatus.BLOCKED, evidence)
    with pytest.raises(ValueError, match="non-CV blocker"):
        ledger.select_cv(MATRIX, _choice(catalog))
    job = ledger.get(MATRIX)
    assert job.status == BatchStatus.BLOCKED
    assert job.latest_evidence == evidence
    assert job.selected_cv is None
    assert ledger.claim_next() is None


@pytest.mark.parametrize(
    "reason",
    [
        "cv_selection_needed",
        "cv_selection_needed: ambiguous role match",
        "selected_cv_missing_or_changed",
    ],
)
def test_explicit_selection_can_resolve_only_cv_specific_blockers(tmp_path, catalog, reason):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=EMBEDDED)])
    ledger.prepare(EMBEDDED, None, reason)
    ledger.select_cv(EMBEDDED, _choice(catalog))
    assert ledger.get(EMBEDDED).status == BatchStatus.PREPARED
    assert ledger.claim_next().canonical_url == EMBEDDED
