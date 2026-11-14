"""Received-your-application is a receipt; receiving a message/CV alone is not."""

from __future__ import annotations

import pytest

from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot

URL = "https://example.test/jobs/backend-developer"


@pytest.mark.parametrize(
    "message",
    [
        "We’ve received your application for the Backend Developer position.",
        "We've received your application for the Backend Developer position.",
        "We have received your application.",
        "Your application has been submitted. A confirmation email was not sent.",
        "We have received your application. Do not reply to this email.",
        "Application submitted; updating your profile is not required.",
    ],
)
def test_received_application_is_positive_and_replays_idempotently(tmp_path, message):
    ledger = BatchLedger(tmp_path / "receipt.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Backend Developer")])
    evidence = ObservedEvidence(kind="page_confirmation", text=message, url=URL)
    assert ledger.record(URL, BatchStatus.SUBMITTED, evidence).status == BatchStatus.SUBMITTED
    replay = ObservedEvidence.model_validate(evidence.model_dump(exclude={"observed_at"}))
    assert ledger.record(URL, BatchStatus.SUBMITTED, replay).latest_evidence == evidence
    assert ledger.export()["counts"]["submitted"] == 1


@pytest.mark.parametrize(
    "message",
    [
        "We have received your message.",
        "We've received your CV.",
        "Your submission was successful.",
        "Your email has been sent. We will get back to you soon.",
        "We have not received your application.",
        "We haven't received your application.",
        "We have received your application draft; please submit it.",
        "Application was not successfully submitted.",
        "No application was submitted.",
        "Application submitted unsuccessfully.",
    ],
)
def test_generic_or_negative_received_text_is_not_a_receipt(tmp_path, message):
    ledger = BatchLedger(tmp_path / "receipt.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL)])
    with pytest.raises(ValueError, match="identify"):
        ledger.record(
            URL,
            BatchStatus.SUBMITTED,
            ObservedEvidence(kind="page_confirmation", text=message, url=URL),
        )


def test_reference_does_not_override_an_explicit_negative_application_result(tmp_path):
    ledger = BatchLedger(tmp_path / "receipt.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Backend Developer")])
    before = ledger.get(URL)
    with pytest.raises(ValueError, match="identify"):
        ledger.record(
            URL,
            BatchStatus.SUBMITTED,
            ObservedEvidence(
                kind="email_confirmation",
                text="Your application could not be submitted.",
                reference="synthetic-message-reference",
                url=URL,
            ),
        )
    assert ledger.get(URL) == before
