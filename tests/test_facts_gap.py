"""Facts-gap classification on synthetic reasons; no real employer or candidate data."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from batch_apply.facts_gap import (
    FIXABLE_KINDS,
    GapJob,
    classify_text,
    domain_outcomes,
    experience_key,
    experience_scope,
    host_family,
    kind_of,
    mine_ledger,
    rank,
    title_of,
    unclassified,
    unlock_estimate,
)
from batch_apply.models import BatchJob, BatchStatus, ObservedEvidence
from batch_apply.urls import job_key


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Required exact SQL years are unknown; no number invented.", {"experience.sql_years"}),
        (
            "Required XML and SQL experience durations are not verified.",
            {"experience.xml_years", "experience.sql_years"},
        ),
        ("Java years of experience not established by the CV.", {"experience.java_years"}),
        (
            "JavaScript experience duration is unsupported.",
            {"experience.javascript_years"},
        ),
        (
            "Exact C++ and C# experience bands are not documented.",
            {"experience.cpp_years", "experience.csharp_years"},
        ),
        (
            "Required .NET experience duration is unknown.",
            {"experience.dotnet_years"},
        ),
        ("Required CI/CD work years are not verified.", {"experience.ci_cd_years"}),
        (
            "Required tenure in an unnamed field is not established.",
            {"experience.unspecified_years"},
        ),
        (
            "Required checkbox accepts Terms of Service and Privacy Policy.",
            {"consent.terms_or_privacy"},
        ),
        ("Terms of Use must be accepted before Submit.", {"consent.terms_or_privacy"}),
        ("Form needs reCAPTCHA before Submit.", {"obstacle.captcha"}),
        ("Comeet explicitly rejected human-check verification.", {"obstacle.captcha"}),
        ("Session verification rejected the application.", {"obstacle.captcha"}),
        ("Email security-code verification required after Submit.", {"obstacle.account_or_login"}),
        ("Account limit of 3 applications reached.", {"obstacle.account_or_login"}),
        ("Resume upload control did not open a file chooser.", {"obstacle.page_or_upload_failure"}),
        (
            "Official application failed to load with ERR_CONNECTION_RESET.",
            {"obstacle.page_or_upload_failure"},
        ),
        (
            "Posting is closed and no longer accepting applications.",
            {"obstacle.closed_or_duplicate"},
        ),
        ("Required desired-start-date field has no answer.", {"availability.notice_or_start"}),
        ("Required notice period is unknown.", {"availability.notice_or_start"}),
        ("Salary expectations are required and unknown.", {"salary.expectation"}),
        ("בבקשה כתוב את ציפיות השכר שלך", {"salary.expectation"}),
        ("Required postal code is unavailable.", {"address.postal_code"}),
        ("Needs a Yes/No on extensive overseas travel.", {"travel.commitment"}),
        ("Count of completed certifications is unsupported.", {"certifications.training"}),
        ("Requires a national ID number.", {"legal.id_clearance_or_status"}),
        ("Requires a B.Sc. in Electrical Engineering.", {"fit.education_requirement"}),
        ("User narrowed scope to engineering roles.", {"fit.role_or_seniority"}),
    ],
)
def test_classify_text_known_phrasings(text, expected):
    assert classify_text(text) >= expected


@pytest.mark.parametrize(
    ("text", "absent"),
    [
        # An unmet minimum is not something a confirmed number can fix, even when the
        # text also says the duration is "not established" (as real ledger text does).
        (
            "Posting requires 4+ years of Python experience; this is not established by the CV.",
            "experience.",
        ),
        (
            "Requires at least three years of QA experience, which is not documented.",
            "experience.",
        ),
        (
            "Exact Python experience is unverified; profile has 1 year, below the minimum.",
            "experience.",
        ),
        # 'privacy notice' is not a notice period.
        ("Please read the privacy notice.", "availability."),
        # Availability to work hybrid is a workplace question.
        ("Required Yes/No availability to work hybrid in the office.", "availability."),
        ("Needs JavaScript years.", "experience.java_years"),
    ],
)
def test_classify_text_avoids_false_positives(text, absent):
    assert not any(key.startswith(absent) for key in classify_text(text))


def test_unmet_minimum_is_a_fit_gap_not_a_fact_gap():
    keys = classify_text("Requires 3+ years Python; profile has under two years.")
    assert keys == {"fit.experience_requirement"}
    assert kind_of("fit.experience_requirement") == "fit"


def test_unspecified_experience_is_not_fixable_by_the_user():
    assert kind_of(experience_key("unspecified")) == "fit"
    assert "manual look" in title_of(experience_key("unspecified"))


def test_hybrid_office_question_is_workplace_not_availability():
    keys = classify_text("Required Yes/No availability to work hybrid in the office.")
    assert "workplace.hybrid_onsite_or_commute" in keys
    assert "availability.notice_or_start" not in keys


def test_empty_or_unrecognised_text_is_unclassified():
    assert classify_text("") == set()
    assert classify_text("The weather was nice.") == set()


def test_requirement_fallback_only_when_nothing_else_matches():
    assert classify_text("The role requires deep domain expertise.") == {"fit.requirements_not_met"}
    assert "fit.requirements_not_met" not in classify_text("Requires a national ID number.")


def test_fit_titles_are_distinct():
    assert title_of("fit.experience_requirement") != title_of("fit.requirements_not_met")


def test_kinds_and_titles():
    assert kind_of("experience.sql_years") == "experience"
    assert kind_of("salary.expectation") == "fact"
    assert kind_of("consent.terms_or_privacy") == "consent"
    assert kind_of("obstacle.captcha") == "obstacle"
    assert kind_of("legal.id_clearance_or_status") == "legal"
    assert title_of("experience.sql_years") == "Years of experience with SQL"
    assert experience_scope("sql") == "years of professional experience with SQL"
    assert {"experience", "fact", "consent"} == FIXABLE_KINDS


@pytest.mark.parametrize(
    ("url", "family"),
    [
        ("https://www.comeet.com/jobs/acme/1/x/AA.1", "comeet"),
        ("https://www.linkedin.com/jobs/view/1/", "linkedin"),
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/acme/1", "lever"),
        ("https://acme.wd1.myworkdayjobs.com/en/x", "workday"),
        ("https://careers.acme.example/jobs/1", "careers.acme.example"),
        ("not a url", "unknown"),
    ],
)
def test_host_family(url, family):
    assert host_family(url) == family


def _job(url, status, reason="", evidence="", company="Acme Ltd"):
    return BatchJob(
        job_id=job_key(url),
        canonical_url=url,
        title="QA Engineer",
        company=company,
        status=status,
        reason=reason,
        latest_evidence=ObservedEvidence(kind="blocked_notice", text=evidence)
        if evidence
        else None,
    )


def _write_ledger(path, jobs, events=()):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            "CREATE TABLE browser_batch_jobs(payload TEXT, canonical_url TEXT);"
            "CREATE TABLE browser_batch_activity(sequence INTEGER PRIMARY KEY, payload TEXT,"
            " event_type TEXT);"
        )
        for job in jobs:
            connection.execute(
                "INSERT INTO browser_batch_jobs VALUES (?,?)",
                (job.model_dump_json(), job.canonical_url),
            )
        for event in events:
            connection.execute(
                "INSERT INTO browser_batch_activity(payload, event_type) VALUES (?, 'blocker')",
                (json.dumps(event),),
            )
        connection.commit()


def test_mine_ledger_current_reason_wins_over_history(tmp_path):
    url = "https://www.comeet.com/jobs/acme/1/x/AA.1"
    job = _job(url, BatchStatus.SKIPPED, reason="Required salary expectation is unknown.")
    event = {"job_id": job.job_id, "details": {"reason": "Earlier a reCAPTCHA appeared."}}
    ledger = tmp_path / "ledger.sqlite3"
    _write_ledger(ledger, [job], [event])
    (mined,) = mine_ledger(ledger)
    assert mined.gaps == {"salary.expectation"}
    assert mined.host == "comeet"


def test_mine_ledger_history_used_only_when_current_reason_is_empty(tmp_path):
    url = "https://jobs.lever.co/acme/1"
    job = _job(url, BatchStatus.BLOCKED, reason="")
    event = {"job_url": url, "details": {"question": "Required notice period?"}}
    ledger = tmp_path / "ledger.sqlite3"
    _write_ledger(ledger, [job], [event])
    (mined,) = mine_ledger(ledger)
    assert mined.gaps == {"availability.notice_or_start"}


def test_mine_ledger_ignores_non_stuck_and_never_writes(tmp_path):
    done = _job(
        "https://www.comeet.com/jobs/acme/2/x/BB.2", BatchStatus.SUBMITTED, reason="captcha"
    )
    stuck = _job("https://www.comeet.com/jobs/acme/3/x/CC.3", BatchStatus.BLOCKED, reason="CAPTCHA")
    ledger = tmp_path / "ledger.sqlite3"
    _write_ledger(ledger, [done, stuck])
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    mined = {job.status: job for job in mine_ledger(ledger)}
    assert mined["submitted"].gaps == frozenset()
    assert mined["blocked"].gaps == {"obstacle.captcha"}
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before


def test_mine_ledger_tolerates_missing_activity_table(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    job = _job("https://www.comeet.com/jobs/acme/4/x/DD.4", BatchStatus.SKIPPED, reason="CAPTCHA")
    with closing(sqlite3.connect(ledger)) as connection:
        connection.execute("CREATE TABLE browser_batch_jobs(payload TEXT, canonical_url TEXT)")
        connection.execute(
            "INSERT INTO browser_batch_jobs VALUES (?,?)",
            (job.model_dump_json(), job.canonical_url),
        )
        connection.commit()
    assert mine_ledger(ledger)[0].gaps == {"obstacle.captcha"}


def test_mine_ledger_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        mine_ledger(tmp_path / "absent.sqlite3")


def _gj(company, status, *gaps, host="comeet"):
    return GapJob(company + "-id", company, status, host, frozenset(gaps))


def test_rank_orders_by_jobs_counts_sole_and_caps_examples():
    jobs = [_gj(f"Co{i}", "blocked", "obstacle.captcha") for i in range(7)] + [
        _gj("A", "skipped", "experience.sql_years"),
        _gj("B", "skipped", "experience.sql_years", "consent.terms_or_privacy"),
        _gj("C", "skipped", "consent.terms_or_privacy"),
        _gj("D", "submitted"),
    ]
    rows = {row.key: row for row in rank(jobs)}
    assert [row.key for row in rank(jobs)][0] == "obstacle.captcha"
    assert rows["obstacle.captcha"].jobs == 7
    assert rows["obstacle.captcha"].sole == 7
    assert len(rows["obstacle.captcha"].companies) == 5
    assert rows["experience.sql_years"].jobs == 2
    assert rows["experience.sql_years"].sole == 1
    assert rows["consent.terms_or_privacy"].jobs == 2


def test_rank_ties_break_by_key_for_stable_output():
    jobs = [_gj("A", "blocked", "salary.expectation"), _gj("B", "blocked", "address.postal_code")]
    assert [row.key for row in rank(jobs)] == ["address.postal_code", "salary.expectation"]


def test_unlock_estimate_needs_every_gap_fixable_and_answered():
    jobs = [
        _gj("A", "skipped", "experience.sql_years"),
        _gj("B", "skipped", "experience.sql_years", "consent.terms_or_privacy"),
        _gj("C", "skipped", "experience.sql_years", "obstacle.captcha"),
        _gj("D", "skipped", "legal.id_clearance_or_status"),
        _gj("E", "skipped"),
        _gj("F", "submitted"),
    ]
    assert unlock_estimate(jobs, {"experience.sql_years"}) == 1
    assert unlock_estimate(jobs, {"experience.sql_years", "consent.terms_or_privacy"}) == 2
    # Answering a legal or obstacle key can never unlock anything.
    assert unlock_estimate(jobs, {"obstacle.captcha", "legal.id_clearance_or_status"}) == 0
    assert unclassified(jobs) == 1


def test_domain_outcomes_split():
    jobs = [
        _gj("A", "submitted"),
        _gj("B", "blocked", "obstacle.captcha"),
        _gj("C", "blocked", "obstacle.account_or_login"),
        _gj("D", "skipped", "salary.expectation"),
        _gj("E", "skipped", "fit.education_requirement"),
        _gj("F", "closed"),
        _gj("G", "submitted", host="lever"),
    ]
    table = {row["host"]: row for row in domain_outcomes(jobs)}
    assert table["comeet"] == {
        "host": "comeet",
        "total": 6,
        "submitted": 1,
        "captcha": 1,
        "account": 1,
        "fixable_by_facts": 1,
        "other_stuck": 1,
        "other": 1,
    }
    assert table["lever"]["submitted"] == 1
    assert domain_outcomes(jobs)[0]["host"] == "comeet"  # most jobs first
