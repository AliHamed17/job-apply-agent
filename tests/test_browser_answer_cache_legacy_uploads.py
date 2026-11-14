"""Historical upload digests bind CV provenance without creating applicant facts."""

import json
import sqlite3
from contextlib import closing

import pytest

from batch_apply.answer_cache import build_answer_cache, lookup_answer
from tests.test_browser_answer_cache import inputs as inputs


def _change_upload(inputs, change):
    with closing(sqlite3.connect(inputs["ledger_path"])) as connection:
        event = json.loads(
            connection.execute(
                "SELECT payload FROM browser_batch_activity WHERE event_type='upload'"
            ).fetchone()[0]
        )
        digest = event.pop("cv_sha256")
        event["details"]["sha256"] = digest
        if change == "wrong_digest":
            event["details"]["sha256"] = "0" * 64
        elif change == "conflicting_top_digest":
            event["cv_sha256"] = digest
            event["details"]["sha256"] = "0" * 64
        elif change == "conflicting_details_digest":
            event["details"]["cv_sha256"] = "0" * 64
        elif change == "wrong_job":
            event["job_id"] = "different-job"
        elif change == "conflicting_job_url":
            event["cv_sha256"] = digest
            event["job_url"] = "https://employer.test/jobs/different-job"
        elif change == "missing_job_id":
            event.pop("job_id")
        elif change == "invalid_digest":
            event["details"]["sha256"] = "not-a-digest"
        elif change == "uppercase_digest":
            event["details"]["sha256"] = digest.upper()
        connection.execute(
            "UPDATE browser_batch_activity SET payload=? WHERE event_type='upload'",
            (json.dumps(event),),
        )
        connection.commit()


@pytest.mark.parametrize("change", ["legacy_digest", "uppercase_digest"])
def test_legacy_upload_digest_attaches_only_historical_cv_provenance(inputs, change):
    original_facts = build_answer_cache(**inputs).facts
    _change_upload(inputs, change)
    cache = build_answer_cache(**inputs)
    assert cache.cv_routes[0]["examples"][0]["upload_event_ids"] == ["upload"]
    assert cache.facts == original_facts
    assert lookup_answer(cache, "experience.python_years", scope="python years") is None


@pytest.mark.parametrize(
    "change",
    [
        "wrong_digest",
        "conflicting_top_digest",
        "conflicting_details_digest",
        "wrong_job",
        "conflicting_job_url",
        "missing_job_id",
        "invalid_digest",
    ],
)
def test_unbound_or_conflicting_upload_provenance_cannot_attach(inputs, change):
    _change_upload(inputs, change)
    cache = build_answer_cache(**inputs)
    assert cache.cv_routes[0]["examples"][0]["upload_event_ids"] == []
