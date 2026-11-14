"""Offline employer exclusion gates using temporary policies and ledgers."""

from __future__ import annotations

import json

import pytest

from batch_apply.catalog import choose_cv_file
from batch_apply.employer_policy import (
    EmployerPolicyError,
    evaluate_employer,
    require_employer_allowed,
)
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from scripts.plan_browser_batch import main


def _policy(tmp_path):
    path = tmp_path / "employer-policy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exclusions": [
                    {
                        "id": "excluded-employer",
                        "employer": "Excluded Wireless",
                        "aliases": ["Excluded Wireless, Inc.", "ExcludedWireless"],
                        "domains": ["excluded.example"],
                        "reason": "Do not apply to this employer.",
                        "source": {"type": "user_instruction", "text": "Do not apply."},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_import_excludes_employer_without_scanning_other_job_text(tmp_path):
    _policy(tmp_path)
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    excluded = "https://www.linkedin.com/jobs/view/1234567890"
    allowed = "https://www.linkedin.com/jobs/view/1234567891"
    ledger.import_tabs(
        [
            TabSnapshot(url=excluded, title="Engineer", company="Excluded Wireless, Inc."),
            TabSnapshot(
                url=allowed,
                title="Engineer",
                company="Allowed Employer",
                description="Relevant experience at Excluded Wireless is welcome.",
            ),
        ]
    )
    assert ledger.get(excluded).status == BatchStatus.BLOCKED
    assert ledger.get(excluded).reason.startswith("employer_excluded:")
    assert ledger.get(allowed).status == BatchStatus.DISCOVERED


@pytest.mark.parametrize(
    "employer,url",
    [
        ("  EXCLUDED—WIRELESS, INC. ", "https://www.linkedin.com/jobs/view/1234567890"),
        ("", "https://careers.excluded.example/jobs/42"),
        ("", "https://jobs.lever.co/excluded-wireless/job-id"),
        ("", "https://www.comeet.com/jobs/excludedwireless/12.345/engineer/AB.CDE"),
    ],
)
def test_alias_domain_and_known_ats_tenant_exclusions(tmp_path, employer, url):
    policy = _policy(tmp_path)
    decision = evaluate_employer(employer, [url], policy_path=policy)
    assert decision["allowed"] is False
    assert decision["matched_rule"] == "excluded-employer"
    with pytest.raises(EmployerPolicyError, match="employer_excluded"):
        require_employer_allowed(employer, [url], policy_path=policy)


@pytest.mark.parametrize(
    "host",
    [
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    ],
)
def test_all_supported_greenhouse_regions_check_the_employer_tenant(tmp_path, host):
    policy = _policy(tmp_path)
    excluded = f"https://{host}/excludedwireless/jobs/42"
    allowed = f"https://{host}/allowed/jobs/42"
    assert not evaluate_employer(urls=[excluded], policy_path=policy)["allowed"]
    assert evaluate_employer(urls=[allowed], policy_path=policy)["allowed"]


def test_eu_greenhouse_exclusion_applies_on_import_and_fresh_redirect(tmp_path):
    _policy(tmp_path)
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    excluded = "https://boards.eu.greenhouse.io/excludedwireless/jobs/42"
    ledger.import_tabs([TabSnapshot(url=excluded, title="Engineer")])
    assert ledger.get(excluded).status == BatchStatus.BLOCKED

    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF-1.4\nsynthetic CV")
    choice = choose_cv_file(tmp_path, cv.name, source="test", reason="Reviewed synthetic role")
    allowed = "https://employer.test/jobs/43"
    ledger.import_tabs([TabSnapshot(url=allowed, title="Engineer", company="Allowed Employer")])
    ledger.prepare(allowed, choice, "Reviewed synthetic role")
    assert ledger.claim_next().canonical_url == allowed
    with pytest.raises(EmployerPolicyError, match="employer_excluded"):
        ledger.pre_submit_check(allowed, observed_urls=[excluded])


@pytest.mark.parametrize(
    "url",
    [
        "https://excluded.example.evil.example/jobs/1",
        "https://allowed.example/jobs/excluded-wireless",
        "https://allowed.example/jobs?companyName=Excluded+Wireless",
        "https://jobs.lever.co/allowed/excluded-wireless",
        "C:/Users/Candidate/OneDrive - Excluded Wireless/CV.pdf",
    ],
)
def test_unrelated_url_components_and_filesystem_names_do_not_exclude(tmp_path, url):
    assert evaluate_employer("Allowed Employer", [url], policy_path=_policy(tmp_path))["allowed"]


def _prepared(tmp_path):
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF-1.4\nfixture")
    choice = choose_cv_file(tmp_path, cv.name, source="test", reason="Reviewed CV")
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    url = "https://www.linkedin.com/jobs/view/1234567890"
    ledger.import_tabs([TabSnapshot(url=url, title="Engineer", company="Excluded Wireless")])
    ledger.prepare(url, choice, "Reviewed CV")
    return ledger, url, choice


def test_policy_added_after_planning_blocks_claim_and_resume(tmp_path):
    ledger, url, choice = _prepared(tmp_path)
    _policy(tmp_path)
    assert ledger.claim_next() is None
    assert ledger.get(url).status == BatchStatus.BLOCKED
    assert ledger.select_cv(url, choice).status == BatchStatus.BLOCKED
    assert ledger.prepare(url, choice, "Try replan").status == BatchStatus.BLOCKED
    with pytest.raises(EmployerPolicyError):
        ledger.resume_blocked(url, choice, "Try resume")


def test_claim_skips_excluded_employer_and_claims_next_allowed_job(tmp_path):
    ledger, excluded, choice = _prepared(tmp_path)
    allowed = "https://www.linkedin.com/jobs/view/1234567891"
    ledger.import_tabs([TabSnapshot(url=allowed, company="Allowed Employer")])
    ledger.prepare(allowed, choice, "Reviewed CV")
    _policy(tmp_path)
    assert ledger.claim_next().canonical_url == allowed
    assert ledger.get(excluded).status == BatchStatus.BLOCKED


@pytest.mark.parametrize(
    "invalid",
    [
        "{",
        "{}",
        '{"schema_version":true,"exclusions":[]}',
        '{"schema_version":1,"exclusions":[{}]}',
    ],
)
def test_malformed_existing_policy_fails_closed_after_planning(tmp_path, invalid):
    ledger, url, _ = _prepared(tmp_path)
    (tmp_path / "employer-policy.json").write_text(invalid, encoding="utf-8")
    assert ledger.claim_next() is None
    assert ledger.get(url).reason.startswith("employer_policy_invalid:")


def test_missing_policy_preserves_existing_behavior(tmp_path):
    ledger, url, _ = _prepared(tmp_path)
    assert not ledger.check_employer(url)["policy_present"]
    assert ledger.claim_next().canonical_url == url


def test_presubmit_reloads_policy_and_checks_observed_employer_and_redirect(tmp_path):
    ledger, url, _ = _prepared(tmp_path)
    ledger.claim_next()
    assert ledger.pre_submit_check(url, observed_urls=[url])["allowed"]
    _policy(tmp_path)
    with pytest.raises(EmployerPolicyError, match="employer_excluded"):
        ledger.pre_submit_check(url, observed_employer="Excluded Wireless")
    # A misleading stored employer cannot allow a redirect to an excluded employer.
    other = BatchLedger(tmp_path / "other.sqlite3")
    other.import_tabs([TabSnapshot(url=url, company="Allowed Employer")])
    with pytest.raises(EmployerPolicyError, match="employer_excluded"):
        other.pre_submit_check(url, observed_urls=["https://excluded.example/apply"])


@pytest.mark.parametrize(
    "status,kind,text",
    [
        (BatchStatus.SUBMITTED, "page_confirmation", "Application submitted"),
        (BatchStatus.SUBMISSION_UNCERTAIN, "submit_clicked", "Clicked Submit; outcome uncertain"),
    ],
)
def test_prior_submissions_and_uncertain_attempts_are_not_rewritten(tmp_path, status, kind, text):
    ledger, url, _ = _prepared(tmp_path)
    ledger.claim_next()
    receipt = ObservedEvidence(kind=kind, text=text)
    ledger.record(url, status, receipt)
    _policy(tmp_path)
    ledger.import_tabs([TabSnapshot(url=url, company="Excluded Wireless")])
    assert ledger.get(url).status == status
    assert ledger.get(url).latest_evidence == receipt
    assert ledger.prepare(url, None, "Replan").status == status
    assert ledger.claim_next() is None


def test_cli_presubmit_check_denies_observed_excluded_employer(tmp_path, capsys):
    ledger, url, _ = _prepared(tmp_path)
    ledger.claim_next()
    _policy(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "pre-submit-check",
                "--ledger",
                str(ledger.path),
                "--url",
                url,
                "--observed-employer",
                "Excluded Wireless",
                "--observed-url",
                url,
            ]
        )
    assert error.value.code == 2
    assert "employer_excluded" in capsys.readouterr().err


def test_presubmit_requires_current_observed_identity(tmp_path):
    ledger, url, _ = _prepared(tmp_path)
    ledger.claim_next()
    with pytest.raises(EmployerPolicyError, match="observed"):
        ledger.pre_submit_check(url)
    with pytest.raises(ValueError):
        ledger.pre_submit_check(url, observed_urls=["C:/private/CV.pdf"])


def test_existing_malformed_policy_blocks_presubmit_without_rewriting_claim(tmp_path):
    ledger, url, _ = _prepared(tmp_path)
    ledger.claim_next()
    (tmp_path / "employer-policy.json").write_text("{", encoding="utf-8")
    with pytest.raises(EmployerPolicyError, match="employer_policy_invalid"):
        ledger.pre_submit_check(url, observed_urls=[url])
    assert ledger.get(url).status == BatchStatus.IN_PROGRESS
