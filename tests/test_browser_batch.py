"""Offline evidence/state tests; no browser, LLM, mail, or real submissions."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from batch_apply.catalog import choose_cv_file, recommend_cv, verify_cv
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from batch_apply.policy import resolve_no_input
from batch_apply.urls import canonical_job_url
from scripts.plan_browser_batch import main, read_snapshot
from submitters.form_brain import AnswerResult, FieldSpec

URL = "https://www.linkedin.com/jobs/view/1234567890"


@pytest.mark.parametrize(
    "change",
    [
        {"url": URL + "?access_token=synthetic-only-token"},
        {"url": "https://user:synthetic-password@example.test/jobs/1"},
        {"description": "password=synthetic-only-password"},
    ],
)
def test_credential_bearing_inventory_never_writes_a_partial_batch(tmp_path, change):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    unsafe = {"url": URL, "title": "Engineer", **change}
    with pytest.raises(ValueError, match="credential|session"):
        ledger.import_tabs(
            [
                TabSnapshot(url="https://employer.test/jobs/safe"),
                TabSnapshot(**unsafe),
            ]
        )
    assert ledger.list_jobs() == []


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://employer.test/jobs/1?access_token=synthetic-only-token"},
        {"text": "password=synthetic-only-password"},
    ],
)
def test_credential_bearing_outcome_is_not_persisted(tmp_path, change):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Engineer")])
    before = ledger.get(URL)
    evidence = ObservedEvidence(kind="blocked_notice", **{"text": "Blocked", **change})
    with pytest.raises(ValueError, match="credential|session"):
        ledger.record(URL, BatchStatus.BLOCKED, evidence)
    assert ledger.get(URL) == before


def test_legacy_credential_payload_is_not_returned_by_get_or_export(tmp_path):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Engineer")])
    legacy = ledger.get(URL).model_dump(mode="json")
    legacy["source_urls"].append(URL + "?access_token=synthetic-only-token")
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute("UPDATE browser_batch_jobs SET payload=?", (json.dumps(legacy),))
        connection.commit()
    for read in [lambda: ledger.get(URL), ledger.export]:
        with pytest.raises(ValueError, match="credential|session"):
            read()


def test_ledger_releases_owned_connections_after_each_operation(tmp_path, monkeypatch):
    original_connect = sqlite3.connect
    connections = []

    class TrackedConnection(sqlite3.Connection):
        explicitly_closed = False

        def close(self):
            self.explicitly_closed = True
            return super().close()

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Engineer")])
    ledger.get(URL)
    ledger.export()
    released = bool(connections) and all(c.explicitly_closed for c in connections)
    for connection in connections:
        connection.close()
    assert released


@pytest.fixture
def cvs(tmp_path):
    folder = tmp_path / "cvs"
    folder.mkdir()
    for role in ("Python_Software_Engineer", "Data_Scientist"):
        (folder / f"{role}_CV.pdf").write_bytes(b"%PDF-1.4\nfixture")
    return folder


def _prepared(tmp_path, cvs):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL, title="Python Software Engineer")])
    choice, reason = recommend_cv(TabSnapshot(url=URL, title="Python Software Engineer"), cvs)
    assert choice is not None
    ledger.prepare(URL, choice, reason)
    return ledger


@pytest.mark.parametrize(
    "url",
    [
        URL + "/?trackingId=a&refId=x#ignored",
        "https://linkedin.com/jobs/search/?currentJobId=1234567890&keywords=Python",
        "https://www.linkedin.com/jobs/view/python-engineer-1234567890/",
    ],
)
def test_linkedin_selected_job_deduplicates(url):
    assert canonical_job_url(url) == URL


def test_canonicalization_keeps_job_parameters_and_sorts():
    assert canonical_job_url("https://Example.com/jobs/?utm_source=x&z=2&gh_jid=5&a=1") == (
        "https://example.com/jobs?a=1&gh_jid=5&z=2"
    )
    assert canonical_job_url("https://example.com/?gh_jid=5") != canonical_job_url(
        "https://example.com/?gh_jid=6"
    )


@pytest.mark.parametrize(
    "base,suffix",
    [
        (
            "https://jobs.eu.lever.co/example-employer/00000000-0000-4000-8000-000000000001",
            "/apply",
        ),
        (
            "https://jobs.eu.lever.co/example-employer/00000000-0000-4000-8000-000000000001",
            "/thanks",
        ),
        ("https://job-boards.eu.greenhouse.io/example-employer/jobs/1234567890", "/confirmation"),
    ],
)
def test_recorded_provider_form_and_confirmation_routes_keep_posting_identity(
    tmp_path, base, suffix
):
    ledger = BatchLedger(tmp_path / "provider.sqlite3")
    ledger.import_tabs([TabSnapshot(url=base)])
    ledger.record(
        base,
        BatchStatus.ALREADY_APPLIED,
        ObservedEvidence(
            kind="already_applied_notice",
            text="Application confirmation observed in recording",
        ),
    )
    result = ledger.import_tabs([TabSnapshot(url=base + suffix + "/?utm_source=linkedin")])
    assert result == {"inserted": 0, "duplicates": 1, "tabs": 1}
    assert canonical_job_url(base + suffix) == base
    assert ledger.get(base + suffix).status == BatchStatus.ALREADY_APPLIED
    assert ledger.claim_next() is None


def test_comeet_referral_does_not_duplicate_recorded_job():
    base = "https://www.comeet.com/jobs/example/AA.001/software-engineer/BB.002"
    assert canonical_job_url(base + "?coref=example-referral&gh_jid=5") == base + "?gh_jid=5"
    # coref is provider-specific; unknown sites may use that parameter as identity.
    assert canonical_job_url("https://example.com/jobs/1?coref=abc") == (
        "https://example.com/jobs/1?coref=abc"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/example-employer/jobs/1234567890/confirmation",
        "https://untrusted.greenhouse.io/example-employer/jobs/1234567890/confirmation",
        "https://jobs.eu.lever.co/example-employer/not-a-posting-id/apply",
        "https://jobs.eu.lever.co/example-employer/00000000-0000-4000-8000-000000000001/other",
    ],
)
def test_provider_suffix_canonicalization_is_narrow(url):
    assert canonical_job_url(url) == url


def test_confirmation_suffix_is_not_success_evidence(tmp_path):
    base = "https://job-boards.eu.greenhouse.io/example-employer/jobs/1234567890"
    ledger = BatchLedger(tmp_path / "provider.sqlite3")
    ledger.import_tabs([TabSnapshot(url=base + "/confirmation")])
    assert ledger.get(base).status == BatchStatus.DISCOVERED
    assert ledger.export()["counts"]["submitted"] == 0


@pytest.mark.parametrize(
    "url", ["file:///private.txt", "javascript:alert(1)", "https://user:secret@example.com/jobs/1"]
)
def test_invalid_inventory_does_not_write_partial_batch(tmp_path, url):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    with pytest.raises(ValueError):
        ledger.import_tabs([TabSnapshot(url=URL), TabSnapshot(url=url)])
    assert ledger.list_jobs() == []


def test_duplicate_tabs_keep_one_record_and_reimport_preserves_submission(tmp_path, cvs):
    ledger = _prepared(tmp_path, cvs)
    outcome = ObservedEvidence(kind="page_confirmation", text="Your application has been submitted")
    ledger.record(URL, BatchStatus.SUBMITTED, outcome)
    result = ledger.import_tabs(
        [
            TabSnapshot(url=URL + "/?trk=jobs", title="Duplicate", tab_id="9"),
            TabSnapshot(url=URL, tab_id="10"),
        ]
    )
    assert result == {"inserted": 0, "duplicates": 2, "tabs": 2}
    job = ledger.list_jobs()[0]
    assert job.status == BatchStatus.SUBMITTED
    assert job.latest_evidence == outcome
    assert job.tab_ids == ["9", "10"]
    assert ledger.claim_next() is None
    assert ledger.prepare(URL, job.selected_cv, "replan").status == BatchStatus.SUBMITTED


def test_click_is_uncertain_and_cannot_be_requeued(tmp_path, cvs):
    ledger = _prepared(tmp_path, cvs)
    ledger.claim_next()
    click = ObservedEvidence(kind="submit_clicked", text="Clicked Submit; no confirmation appeared")
    with pytest.raises(ValueError, match="confirmation"):
        ledger.record(URL, BatchStatus.SUBMITTED, click)
    ledger.record(URL, BatchStatus.SUBMISSION_UNCERTAIN, click)
    with pytest.raises(ValueError, match="Reconcile"):
        ledger.record(
            URL, BatchStatus.BLOCKED, ObservedEvidence(kind="blocked_notice", text="Timeout")
        )
    with pytest.raises(ValueError, match="Cannot replace"):
        ledger.select_cv(URL, ledger.get(URL).selected_cv)
    assert ledger.claim_next() is None
    ledger.record(
        URL,
        BatchStatus.SUBMITTED,
        ObservedEvidence(
            kind="email_confirmation",
            text="Application received",
            reference="mail-evidence-1",
        ),
    )
    assert ledger.get(URL).status == BatchStatus.SUBMITTED


def test_submitted_requires_confirmation_words_or_reference(tmp_path, cvs):
    ledger = _prepared(tmp_path, cvs)
    with pytest.raises(ValueError, match="identify"):
        ledger.record(
            URL,
            BatchStatus.SUBMITTED,
            ObservedEvidence(
                kind="page_confirmation",
                text="Apply for this position",
            ),
        )


def test_observed_prior_application_is_terminal_without_claiming_new_submission(tmp_path):
    ledger = BatchLedger(tmp_path / "batch.sqlite3")
    ledger.import_tabs([TabSnapshot(url=URL)])
    ledger.record(
        URL,
        BatchStatus.ALREADY_APPLIED,
        ObservedEvidence(
            kind="already_applied_notice",
            text="Applied: visible in recording at 04:32",
        ),
    )
    exported = ledger.export()
    assert exported["counts"]["already_applied"] == 1
    assert exported["counts"]["submitted"] == 0
    with pytest.raises(ValueError, match="terminal"):
        ledger.record(
            URL, BatchStatus.BLOCKED, ObservedEvidence(kind="blocked_notice", text="Retry")
        )


def test_claim_is_transactional_and_changed_cv_blocks_before_upload(tmp_path, cvs):
    ledger = _prepared(tmp_path, cvs)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: ledger.claim_next(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert ledger.get(URL).status == BatchStatus.IN_PROGRESS
    other_url = "https://example.com/jobs/2"
    ledger.import_tabs([TabSnapshot(url=other_url)])
    choice = ledger.get(URL).selected_cv
    ledger.prepare(other_url, choice, "same CV")
    (cvs / choice.file).write_bytes(b"%PDF-1.4\nchanged")
    assert not verify_cv(choice)
    claimed = ledger.claim_next()
    assert claimed.status == BatchStatus.BLOCKED
    assert claimed.reason == "selected_cv_missing_or_changed"


def test_cv_selection_is_scoped_and_records_file_provenance(tmp_path, cvs):
    outside = tmp_path / "other.pdf"
    outside.write_bytes(b"%PDF-1.4\noutside")
    with pytest.raises(ValueError, match="inside"):
        choose_cv_file(cvs, "../other.pdf", source="llm", reason="outside")
    choice, _ = recommend_cv(TabSnapshot(url=URL, title="Python Software Engineer"), cvs)
    assert choice.file == "Python_Software_Engineer_CV.pdf"
    assert len(choice.sha256) == 64
    assert choice.source == "filename_role_match"
    assert "python" in choice.reason


def test_weak_and_ambiguous_cv_matches_abstain(cvs):
    assert recommend_cv(TabSnapshot(url=URL, title="Accountant"), cvs)[0] is None
    (cvs / "Python_Software_Engineer_Alternate_CV.pdf").write_bytes(b"%PDF-1.4\nx")
    # Exact copies of the role with no distinguishing title evidence are ambiguous.
    (cvs / "Python-Software_Engineer_CV.pdf").write_bytes(b"%PDF-1.4\nx")
    assert recommend_cv(TabSnapshot(url=URL, title="Python Software Engineer"), cvs)[0] is None


@pytest.mark.parametrize(
    "field,action,value",
    [
        (FieldSpec("Optional cover note", "textarea", required=False), "skip", None),
        (FieldSpec("Years using Rust", "number", required=True), "block_job", None),
        (FieldSpec("Authorized to work?", "radio", ["Yes", "No"], True), "block_job", None),
        (
            FieldSpec("Gender", "select", ["Male", "Female", "Prefer not to say"], True),
            "fill",
            "Prefer not to say",
        ),
        (
            FieldSpec("Additional information", "textarea", required=True),
            "fill",
            "No additional information.",
        ),
    ],
)
def test_no_input_unknown_policy(field, action, value):
    decision = resolve_no_input(field)
    assert decision.action == action
    assert decision.value == value


def test_no_input_rejects_gibberish_invalid_options_and_unconfirmed_declarations():
    assert (
        resolve_no_input(
            FieldSpec("Experience", "text", required=True), AnswerResult(",", "llm", True)
        ).action
        == "block_job"
    )
    question = FieldSpec("Visa sponsorship required?", "radio", ["Yes", "No"], True)
    assert resolve_no_input(question, AnswerResult("No", "llm", True)).action == "block_job"
    assert resolve_no_input(question, AnswerResult("No", "user_confirmed", True)).value == "No"
    assert (
        resolve_no_input(question, AnswerResult("Maybe", "user_confirmed", True)).action
        == "block_job"
    )


def test_cli_plan_explicit_llm_selection_and_prior_application(tmp_path, cvs, capsys):
    snapshot = tmp_path / "tabs.json"
    selections = tmp_path / "selections.json"
    ledger_path = tmp_path / "batch.sqlite3"
    output = tmp_path / "plan.json"
    snapshot.write_text(json.dumps({"tabs": [{"url": URL, "title": "Unfamiliar role"}]}))
    selections.write_text(
        json.dumps(
            [
                {
                    "url": URL,
                    "cv_file": "Data_Scientist_CV.pdf",
                    "reason": "LLM read the posting and selected the data-science CV",
                }
            ]
        )
    )
    assert len(read_snapshot(snapshot)) == 1
    assert (
        main(
            [
                "plan",
                str(snapshot),
                "--cv-dir",
                str(cvs),
                "--ledger",
                str(ledger_path),
                "--selections",
                str(selections),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text())
    assert payload["counts"]["prepared"] == 1
    assert payload["counts"]["submitted"] == 0
    assert payload["jobs"][0]["selected_cv"]["source"] == "llm_browser_review"
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"kind": "already_applied_notice", "text": "Applied yesterday"}))
    assert (
        main(
            [
                "record",
                "--ledger",
                str(ledger_path),
                "--url",
                URL,
                "--status",
                "already_applied",
                "--evidence-json",
                str(evidence),
            ]
        )
        == 0
    )
    assert BatchLedger(ledger_path).claim_next() is None
    capsys.readouterr()
