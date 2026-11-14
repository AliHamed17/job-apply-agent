"""Offline tests for the cross-platform duplicate guard (temporary ledgers only)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from batch_apply.duplicate_guard import (
    ProbableDuplicateError,
    find_probable_duplicates,
    normalize_identity,
    require_not_probable_duplicate,
)


def _ledger(tmp_path, jobs):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE browser_batch_jobs(job_id TEXT PRIMARY KEY, canonical_url TEXT, "
            "status TEXT, payload TEXT)"
        )
        for job_id, status, company, title, url in jobs:
            payload = json.dumps({"company": company, "title": title})
            connection.execute(
                "INSERT INTO browser_batch_jobs VALUES (?, ?, ?, ?)",
                (job_id, url, status, payload),
            )
    return path


COMEET = "https://www.comeet.com/jobs/example/1/qa-engineer/2"
LINKEDIN = "https://www.linkedin.com/jobs/view/123/"


def test_same_job_on_another_site_is_found(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", "Example Ltd.", "QA Engineer", COMEET)])
    found = find_probable_duplicates(path, "Example", "QA Engineer (M/F)", exclude_job_id="b")
    assert [m["job_id"] for m in found] == ["a"]
    assert found[0]["canonical_url"] == COMEET


def test_require_raises_and_names_the_earlier_application(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", "Example", "QA Engineer", COMEET)])
    with pytest.raises(ProbableDuplicateError, match="already has a submitted application"):
        require_not_probable_duplicate(path, "Example Inc", "QA Engineer", job_id="b")


def test_reviewed_distinct_requisition_is_allowed_and_still_reported(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", "Example", "QA Engineer", COMEET)])
    matches = require_not_probable_duplicate(
        path, "Example", "QA Engineer", job_id="b", reviewed_distinct=True
    )
    assert [m["job_id"] for m in matches] == ["a"]


@pytest.mark.parametrize("status", ["skipped", "blocked", "closed", "prepared", "discovered"])
def test_jobs_that_were_never_applied_to_do_not_block(tmp_path, status):
    path = _ledger(tmp_path, [("a", status, "Example", "QA Engineer", COMEET)])
    assert find_probable_duplicates(path, "Example", "QA Engineer", exclude_job_id="b") == []


@pytest.mark.parametrize("status", ["submitted", "already_applied", "submission_uncertain"])
def test_applied_or_possibly_applied_jobs_block(tmp_path, status):
    path = _ledger(tmp_path, [("a", status, "Example", "QA Engineer", COMEET)])
    assert find_probable_duplicates(path, "Example", "QA Engineer", exclude_job_id="b")


def test_another_lane_working_on_the_same_job_blocks(tmp_path):
    path = _ledger(tmp_path, [("a", "in_progress", "Example", "QA Engineer", COMEET)])
    assert find_probable_duplicates(path, "Example", "QA Engineer", exclude_job_id="b")


def test_the_job_itself_never_blocks_itself(tmp_path):
    path = _ledger(tmp_path, [("a", "in_progress", "Example", "QA Engineer", LINKEDIN)])
    assert find_probable_duplicates(path, "Example", "QA Engineer", exclude_job_id="a") == []


def test_different_titles_or_employers_do_not_match(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", "Example", "QA Engineer", COMEET)])
    assert find_probable_duplicates(path, "Example", "QA Automation Engineer") == []
    assert find_probable_duplicates(path, "Other", "QA Engineer") == []


def test_unknown_company_or_title_never_matches(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", "", "", COMEET)])
    assert find_probable_duplicates(path, "", "") == []
    assert normalize_identity("", "QA Engineer") == ""


def test_hebrew_names_and_gender_markers_normalize(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", 'אקמי בע"מ', "מהנדס/ת בדיקות", COMEET)])
    assert find_probable_duplicates(path, "אקמי", "מהנדס בדיקות")


def test_ledger_is_opened_read_only(tmp_path):
    path = _ledger(tmp_path, [("a", "submitted", "Example", "QA Engineer", COMEET)])
    before = path.read_bytes()
    find_probable_duplicates(path, "Example", "QA Engineer")
    assert path.read_bytes() == before
