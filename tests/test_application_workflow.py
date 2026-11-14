"""Synthetic, offline work packets preserve evidence and application state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import batch_apply.answer_cache as cache_module
from batch_apply.answer_cache import build_answer_cache
from batch_apply.catalog import choose_cv_file
from batch_apply.models import BatchJob, BatchStatus, ObservedEvidence
from batch_apply.urls import job_key
from batch_apply.workflow import ObservedApplication, build_knowledge, prepare_packet
from scripts.application_workflow import main


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_job(database: Path, job: BatchJob) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO browser_batch_jobs VALUES (?,?)",
            (job.model_dump_json(), job.canonical_url),
        )
        connection.commit()


@pytest.fixture
def workflow_inputs(tmp_path, monkeypatch):
    directory = tmp_path / "cvs"
    directory.mkdir()
    cv = directory / "Synthetic_Data_Engineer_CV.pdf"
    cv.write_bytes(b"%PDF-1.4\nsynthetic unchanged resume")
    choice = choose_cv_file(
        directory, cv.name, source="llm_browser_review", reason="Reviewed synthetic data role"
    )
    historical_url = "https://jobs.lever.co/synthetic-employer/historical-job"
    receipt = tmp_path / "receipt.txt"
    receipt.write_text("Synthetic application submitted successfully", encoding="utf-8")
    historical = BatchJob(
        job_id=job_key(historical_url),
        canonical_url=historical_url,
        title="Data Engineer",
        company="Synthetic Employer",
        status=BatchStatus.SUBMITTED,
        selected_cv=choice,
        latest_evidence=ObservedEvidence(
            kind="page_confirmation",
            text="Synthetic application submitted successfully",
            artifact=str(receipt),
            url=historical_url,
        ),
    )
    database = tmp_path / "ledger.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE browser_batch_jobs(payload TEXT,canonical_url TEXT);"
            "CREATE TABLE browser_batch_activity(sequence INTEGER PRIMARY KEY,payload TEXT,"
            "event_type TEXT);"
        )
        for sequence, typ in enumerate(
            ["observation", "form_field", "upload", "submit_attempt", "confirmation"], start=1
        ):
            event = {
                "event_id": f"synthetic-{sequence}",
                "event_type": typ,
                "job_id": historical.job_id,
                "job_url": historical_url,
                "run_id": "synthetic-run",
                "cv_sha256": choice.sha256,
                "entered": {
                    "password": "historical-credential-not-imported",
                    "python_years": 19,
                    "private_note": "raw-answer-not-imported",
                },
                "observed": {"national_id": "raw-identifier-not-imported"},
                "details": {"reason": "Historical event, not an applicant fact"},
            }
            connection.execute(
                "INSERT INTO browser_batch_activity(sequence,payload,event_type) VALUES (?,?,?)",
                (sequence, json.dumps(event), typ),
            )
        connection.commit()
    _insert_job(database, historical)

    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {
                "personal": {
                    "name": "Test Applicant",
                    "email": "person@test.invalid",
                    "phone": "+15551234567",
                    "national_id": "overlay-id-not-imported",
                },
                "links": {
                    "linkedin": "https://www.linkedin.com/in/test-applicant/",
                    "github": "https://github.com/test-applicant",
                    "portfolio": "https://portfolio.test.invalid/",
                },
                "provenance": {
                    "personal": "Synthetic reviewed contact",
                    "linkedin_github": "Synthetic reviewed links",
                    "portfolio": "Synthetic reviewed portfolio",
                },
                "credentials": {"password": "overlay-credential-not-imported"},
            }
        ),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "personal:\n  location: Test Town, Region\n"
        "attachments:\n  - path: absent-academic-document.pdf\n",
        encoding="utf-8",
    )
    proofs = [tmp_path / "original.bin", tmp_path / "reviewed-frame.bin"]
    for proof in proofs:
        proof.write_bytes(proof.name.encode())
    manifest = tmp_path / "reviewed-facts.json"
    manifest.write_text(
        json.dumps(
            {
                "facts": [
                    {
                        "id": "query-answer",
                        "key": "experience.query_language_years",
                        "value": 2,
                        "scope": "complex_enterprise_query_years",
                        "reviewed": True,
                        "inherited_prefill": False,
                        "source_type": "recording_active_answer",
                        "active_entry_observed": True,
                        "proofs": [{"path": str(p), "sha256": _digest(p)} for p in proofs],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "employer-policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exclusions": [
                    {
                        "id": "excluded",
                        "employer": "Excluded Employer",
                        "aliases": [],
                        "domains": ["excluded.test.invalid"],
                        "reason": "Synthetic user exclusion",
                        "source": {
                            "type": "user_instruction",
                            "text": "Do not apply to this employer.",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cache_module, "_one_page", lambda _: True)
    cache = build_answer_cache(
        ledger_path=database,
        overlay_path=overlay,
        profile_path=profile,
        cv_directory=directory,
        verified_facts_path=manifest,
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(cache.model_dump_json(), encoding="utf-8")
    return {
        "ledger_path": database,
        "cache_path": cache_path,
        "cv_directory": directory,
        "policy_path": policy,
        "overlay": overlay,
        "profile": profile,
        "manifest": manifest,
        "cv": cv,
        "choice": choice,
        "historical": historical,
    }


def _kwargs(inputs):
    return {
        key: inputs[key]
        for key in (
            "ledger_path",
            "cache_path",
            "cv_directory",
            "policy_path",
        )
    }


def _application(inputs, **changes):
    raw = {
        "job": {
            "url": "https://employer.test.invalid/jobs/new-role",
            "title": "Data Engineer",
            "company": "Allowed Employer",
        },
        "cv_file": inputs["cv"].name,
        "cv_reason": "Actual job and unchanged CV reviewed",
        "fields": [],
    }
    raw.update(changes)
    return ObservedApplication.model_validate(raw)


def _assert_passive(packet):
    assert packet["submission_ready"] is False
    assert packet["browser_actions_performed"] is False
    assert packet["ledger_modified"] is False
    assert packet["requires_live_review"] is True


def test_knowledge_uses_step_types_without_ingesting_raw_answers(workflow_inputs):
    inputs = workflow_inputs
    before = _digest(inputs["ledger_path"])
    knowledge = build_knowledge(**_kwargs(inputs))
    assert _digest(inputs["ledger_path"]) == before
    assert knowledge["activity_event_count"] == 5
    assert knowledge["counts"] == {"submitted": 1}
    example = knowledge["source_provider_patterns"]["Lever"]["examples"][0]
    assert example["step_types"] == [
        "observation",
        "form_field",
        "upload",
        "submit_attempt",
        "confirmation",
    ]
    assert example["receipt_artifact"] == inputs["historical"].latest_evidence.artifact
    rendered = json.dumps(knowledge)
    for private in [
        "historical-credential-not-imported",
        "raw-answer-not-imported",
        "raw-identifier-not-imported",
        "overlay-id-not-imported",
        "overlay-credential-not-imported",
        "absent-academic-document",
        "person@test.invalid",
        "+15551234567",
    ]:
        assert private not in rendered
    assert not any(
        fact["key"] == "experience.python_years" for fact in knowledge["verified_fact_index"]
    )


def test_preparing_packet_keeps_ledger_and_saved_receipt_unchanged(workflow_inputs):
    inputs = workflow_inputs
    before = _digest(inputs["ledger_path"])
    receipt = Path(inputs["historical"].latest_evidence.artifact)
    receipt_before = _digest(receipt)
    packet = prepare_packet(_application(inputs), **_kwargs(inputs))
    assert packet["next_action"] == "review_and_claim"
    assert packet["cv_candidate"]["sha256"] == inputs["choice"].sha256
    assert _digest(inputs["ledger_path"]) == before
    assert _digest(receipt) == receipt_before
    _assert_passive(packet)


def test_changed_source_disables_packet_answers(workflow_inputs):
    inputs = workflow_inputs
    inputs["overlay"].write_text("changed source", encoding="utf-8")
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "email",
                    "label": "Email",
                    "required": True,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    assert packet["next_action"] == "refresh_verified_sources"
    assert "cache_source_changed" in packet["source_issues"]
    assert packet["fields"] == [] and packet["cv_candidate"] is None
    _assert_passive(packet)


def test_employer_exclusion_precedes_ledger_or_cache_access(workflow_inputs):
    inputs = workflow_inputs
    kwargs = _kwargs(inputs)
    kwargs.update(
        ledger_path=inputs["ledger_path"].with_name("absent.sqlite3"),
        cache_path=inputs["cache_path"].with_name("absent.json"),
    )
    packet = prepare_packet(
        _application(
            inputs,
            job={
                "url": "https://excluded.test.invalid/jobs/1",
                "company": "Excluded Employer",
                "title": "Data Engineer",
            },
        ),
        **kwargs,
    )
    assert packet["next_action"] == "skip_excluded_employer"
    assert packet["employer_check"]["allowed"] is False
    assert packet["fields"] == []
    _assert_passive(packet)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("submitted", "skip_existing_outcome"),
        ("already_applied", "skip_existing_outcome"),
        ("closed", "skip_existing_outcome"),
        ("skipped", "skip_existing_outcome"),
        ("submission_uncertain", "reconcile_existing_attempt"),
        ("in_progress", "reconcile_existing_attempt"),
        ("blocked", "review_existing_blocker"),
    ],
)
@pytest.mark.parametrize("through_alias", [False, True])
def test_existing_identity_gates_before_answers(workflow_inputs, status, expected, through_alias):
    inputs = workflow_inputs
    url = "https://employer.test.invalid/jobs/new-role"
    primary = "https://jobs.lever.co/allowed/primary-role" if through_alias else url
    _insert_job(
        inputs["ledger_path"],
        BatchJob(
            job_id=job_key(primary),
            canonical_url=primary,
            company="Allowed Employer",
            title="Data Engineer",
            status=BatchStatus(status),
            source_urls=[url] if through_alias else [],
        ),
    )
    before = _digest(inputs["ledger_path"])
    kwargs = _kwargs(inputs)
    kwargs["cache_path"] = inputs["cache_path"].with_name("not-needed.json")
    packet = prepare_packet(_application(inputs), **kwargs)
    assert packet["next_action"] == expected
    assert packet["existing_job"]["status"] == status
    assert packet["fields"] == []
    assert _digest(inputs["ledger_path"]) == before
    _assert_passive(packet)


def test_stored_employer_exclusion_survives_misleading_observed_label(workflow_inputs):
    inputs = workflow_inputs
    alias = "https://employer.test.invalid/jobs/new-role"
    primary = "https://jobs.lever.co/excluded/primary"
    _insert_job(
        inputs["ledger_path"],
        BatchJob(
            job_id=job_key(primary),
            canonical_url=primary,
            company="Excluded Employer",
            title="Data Engineer",
            status=BatchStatus.PREPARED,
            source_urls=[alias],
        ),
    )
    packet = prepare_packet(_application(inputs), **_kwargs(inputs))
    assert packet["next_action"] == "skip_excluded_employer"
    _assert_passive(packet)


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Full name", "Test Applicant"),
        ("First name", "Test"),
        ("Last name", "Applicant"),
        ("Email address *", "person@test.invalid"),
        ("Phone number", "+15551234567"),
        ("LinkedIn profile", "https://www.linkedin.com/in/test-applicant/"),
        ("City", "Test Town, Region"),
    ],
)
def test_exact_contact_fields_resolve_to_bound_evidence(workflow_inputs, label, expected):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "contact",
                    "label": label,
                    "kind": "text",
                    "required": True,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    decision = packet["fields"][0]
    assert decision["action"] == "fill" and decision["value"] == expected
    assert decision["source_path"] and decision["source_selector"]
    _assert_passive(packet)


@pytest.mark.parametrize(
    "label",
    [
        "Gross salary expectation",
        "Years of Python experience",
        "Email experience years",
    ],
)
def test_required_unsupported_fields_cannot_use_prefill_or_contact_substrings(
    workflow_inputs, label
):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "unsupported",
                    "label": label,
                    "kind": "number",
                    "required": True,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    assert packet["next_action"] == "block_unsupported_required_fields"
    assert packet["fields"][0]["action"] == "block_job"
    assert packet["fields"][0]["value"] is None
    assert packet["fields"][0]["fact_key"] is None
    _assert_passive(packet)


def test_reviewed_experience_metadata_cannot_override_exact_email_label(workflow_inputs):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "email",
                    "label": "Email",
                    "kind": "text",
                    "required": True,
                    "fact_key": "experience.query_language_years",
                    "scope": "complex_enterprise_query_years",
                    "meaning_reviewed": True,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    assert packet["next_action"] == "block_unsupported_required_fields"
    decision = packet["fields"][0]
    assert decision["action"] == "block_job"
    assert decision["value"] is None
    assert decision["fact_key"] is None
    _assert_passive(packet)


def test_email_label_on_file_control_cannot_emit_text_fill(workflow_inputs):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "file",
                    "label": "Email",
                    "kind": "file",
                    "required": True,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    decision = packet["fields"][0]
    assert decision["action"] == "block_job"
    assert decision["value"] is None
    assert decision["fact_key"] is None
    assert packet["next_action"] == "block_unsupported_required_fields"
    _assert_passive(packet)


@pytest.mark.parametrize(
    "reviewed,scope,expected",
    [
        (True, "complex_enterprise_query_years", "fill"),
        (False, "complex_enterprise_query_years", "block_job"),
        (True, "all_database_administration_years", "block_job"),
    ],
)
def test_experience_requires_explicit_review_and_exact_scope(
    workflow_inputs, reviewed, scope, expected
):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "query-years",
                    "label": "Years with complex enterprise queries",
                    "kind": "number",
                    "required": True,
                    "fact_key": "experience.query_language_years",
                    "scope": scope,
                    "meaning_reviewed": reviewed,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    decision = packet["fields"][0]
    assert decision["action"] == expected
    assert decision["value"] == ("2" if expected == "fill" else None)
    if expected == "fill":
        assert decision["source_selector"] == "facts.query-answer"
    else:
        assert packet["next_action"] == "block_unsupported_required_fields"
    _assert_passive(packet)


def test_missing_explicit_cv_requires_review_instead_of_prepared_attachment(workflow_inputs):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(inputs, cv_file="Missing_Resume_CV.pdf"), **_kwargs(inputs)
    )
    assert packet["next_action"] == "review_and_select_cv"
    assert packet["cv_candidate"] is None
    _assert_passive(packet)


def test_unmatched_role_requires_cv_selection(workflow_inputs):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            cv_file="",
            job={
                "url": "https://employer.test.invalid/jobs/new-role",
                "company": "Allowed Employer",
                "title": "Veterinary Surgeon",
                "description": "Clinical veterinary practice",
            },
        ),
        **_kwargs(inputs),
    )
    assert packet["next_action"] == "review_and_select_cv"
    assert packet["cv_candidate"] is None
    _assert_passive(packet)


@pytest.mark.parametrize("label", ["Resume", "CV *", "Upload resume", "Curriculum vitae"])
def test_resume_attachment_uses_verified_cv_identity(workflow_inputs, label):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "attachment",
                    "label": label,
                    "kind": "file",
                    "required": True,
                }
            ],
        ),
        **_kwargs(inputs),
    )
    assert packet["next_action"] == "review_and_claim"
    decision = packet["fields"][0]
    assert decision["action"] == "upload_cv"
    assert decision["value"] == str(inputs["cv"])
    assert decision["source"] == "verified_cv_identity"
    assert packet["cv_candidate"]["sha256"] == inputs["choice"].sha256
    _assert_passive(packet)


def test_other_required_attachment_cannot_be_filled_with_cv(workflow_inputs):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {"field_id": "resume", "label": "Resume", "kind": "file", "required": True},
                {
                    "field_id": "certificate",
                    "label": "Security clearance certificate",
                    "kind": "file",
                    "required": True,
                },
            ],
        ),
        **_kwargs(inputs),
    )
    assert packet["fields"][0]["action"] == "upload_cv"
    assert packet["fields"][1]["action"] == "block_job"
    assert packet["fields"][1]["value"] is None
    assert packet["next_action"] == "block_unsupported_required_fields"
    _assert_passive(packet)


def test_resume_attachment_does_not_clear_required_salary_blocker(workflow_inputs):
    inputs = workflow_inputs
    packet = prepare_packet(
        _application(
            inputs,
            fields=[
                {"field_id": "resume", "label": "Resume", "kind": "file", "required": True},
                {
                    "field_id": "salary",
                    "label": "Salary expectation",
                    "kind": "number",
                    "required": True,
                },
            ],
        ),
        **_kwargs(inputs),
    )
    assert packet["fields"][0]["action"] == "upload_cv"
    assert packet["fields"][1]["action"] == "block_job"
    assert packet["next_action"] == "block_unsupported_required_fields"
    _assert_passive(packet)


def test_stored_changed_cv_requires_review_without_ledger_change(workflow_inputs):
    inputs = workflow_inputs
    url = "https://employer.test.invalid/jobs/new-role"
    changed_choice = inputs["choice"].model_copy(update={"sha256": "0" * 64})
    _insert_job(
        inputs["ledger_path"],
        BatchJob(
            job_id=job_key(url),
            canonical_url=url,
            title="Data Engineer",
            company="Allowed Employer",
            status=BatchStatus.PREPARED,
            selected_cv=changed_choice,
        ),
    )
    before = _digest(inputs["ledger_path"])
    packet = prepare_packet(_application(inputs, cv_file=""), **_kwargs(inputs))
    assert packet["next_action"] == "review_changed_cv"
    assert packet["cv_candidate"] is None
    assert _digest(inputs["ledger_path"]) == before
    _assert_passive(packet)


def test_duplicate_observed_field_identifiers_rejected(workflow_inputs):
    inputs = workflow_inputs
    application = _application(
        inputs,
        fields=[
            {"field_id": "same", "label": "Email"},
            {"field_id": "same", "label": "Phone"},
        ],
    )
    with pytest.raises(ValueError, match="identifiers must be unique"):
        prepare_packet(application, **_kwargs(inputs))


def test_cli_refresh_and_prepare_are_private_read_only_handoffs(workflow_inputs, tmp_path, capsys):
    inputs = workflow_inputs
    knowledge_path = tmp_path / "knowledge.json"
    cache_path = tmp_path / "fresh-cache.json"
    before = _digest(inputs["ledger_path"])
    assert (
        main(
            [
                "refresh",
                "--ledger",
                str(inputs["ledger_path"]),
                "--overlay",
                str(inputs["overlay"]),
                "--profile",
                str(inputs["profile"]),
                "--verified-facts",
                str(inputs["manifest"]),
                "--cv-dir",
                str(inputs["cv_directory"]),
                "--policy",
                str(inputs["policy_path"]),
                "--cache",
                str(cache_path),
                "--output",
                str(knowledge_path),
            ]
        )
        == 0
    )
    observed = tmp_path / "observed.json"
    observed.write_text(
        _application(
            inputs,
            fields=[
                {
                    "field_id": "email",
                    "label": "Email",
                    "required": True,
                }
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    output = tmp_path / "work-packet.json"
    assert (
        main(
            [
                "prepare",
                "--knowledge",
                str(knowledge_path),
                "--input",
                str(observed),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    packet = json.loads(output.read_text(encoding="utf-8"))
    _assert_passive(packet)
    assert packet["fields"][0]["value"] == "person@test.invalid"
    assert _digest(inputs["ledger_path"]) == before
    logged = capsys.readouterr().out
    assert "person@test.invalid" not in logged and "Test Applicant" not in logged
    assert "historical-credential-not-imported" not in logged


def test_cli_prevents_overwriting_observed_input(workflow_inputs, tmp_path):
    inputs = workflow_inputs
    knowledge_path = tmp_path / "knowledge.json"
    knowledge_path.write_text(json.dumps(build_knowledge(**_kwargs(inputs))), encoding="utf-8")
    observed = tmp_path / "observed.json"
    observed.write_text(_application(inputs).model_dump_json(), encoding="utf-8")
    before = _digest(observed)
    with pytest.raises(SystemExit) as failure:
        main(
            [
                "prepare",
                "--knowledge",
                str(knowledge_path),
                "--input",
                str(observed),
                "--output",
                str(observed),
            ]
        )
    assert failure.value.code == 2
    assert _digest(observed) == before


@pytest.mark.parametrize("manifest_source", ["existing_cache", "conventional_file"])
def test_refresh_without_manifest_flag_preserves_reviewed_experience(
    workflow_inputs, tmp_path, manifest_source
):
    inputs = workflow_inputs
    cache_path = inputs["cache_path"]
    if manifest_source == "conventional_file":
        conventional = cache_path.parent / "verified-answer-facts.json"
        conventional.write_bytes(inputs["manifest"].read_bytes())
        cache_path = tmp_path / "new-cache.json"
    knowledge_path = tmp_path / "refreshed-knowledge.json"
    before = _digest(inputs["ledger_path"])
    assert (
        main(
            [
                "refresh",
                "--ledger",
                str(inputs["ledger_path"]),
                "--overlay",
                str(inputs["overlay"]),
                "--profile",
                str(inputs["profile"]),
                "--cv-dir",
                str(inputs["cv_directory"]),
                "--policy",
                str(inputs["policy_path"]),
                "--cache",
                str(cache_path),
                "--output",
                str(knowledge_path),
            ]
        )
        == 0
    )
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    fact = next(fact for fact in cache["facts"] if fact["key"] == "experience.query_language_years")
    assert fact["value"] == 2
    assert fact["scope"] == "complex_enterprise_query_years"
    knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
    assert any(
        fact["key"] == "experience.query_language_years"
        for fact in knowledge["verified_fact_index"]
    )
    assert _digest(inputs["ledger_path"]) == before


@pytest.mark.parametrize("destination", ["new_manifest", "new_proof"])
def test_prepare_cannot_overwrite_new_live_cache_source_absent_from_old_knowledge(
    workflow_inputs,
    tmp_path,
    destination,
):
    inputs = workflow_inputs
    knowledge = build_knowledge(**_kwargs(inputs))
    knowledge_path = tmp_path / "old-knowledge.json"
    knowledge_path.write_text(json.dumps(knowledge), encoding="utf-8")
    new_proof = tmp_path / "new-reviewed-proof.json"
    new_proof.write_text('{"review":"synthetic additional source"}', encoding="utf-8")
    new_manifest = tmp_path / "new-reviewed-facts.json"
    reviewed = json.loads(inputs["manifest"].read_text(encoding="utf-8"))
    reviewed["facts"][0]["proofs"].append(
        {
            "path": str(new_proof),
            "sha256": _digest(new_proof),
        }
    )
    new_manifest.write_text(json.dumps(reviewed), encoding="utf-8")
    assert str(new_manifest) not in knowledge["protected_source_paths"]
    assert str(new_proof) not in knowledge["protected_source_paths"]
    updated_cache = build_answer_cache(
        ledger_path=inputs["ledger_path"],
        overlay_path=inputs["overlay"],
        profile_path=inputs["profile"],
        cv_directory=inputs["cv_directory"],
        verified_facts_path=new_manifest,
    )
    inputs["cache_path"].write_text(updated_cache.model_dump_json(), encoding="utf-8")
    observed = tmp_path / "observed-new-source.json"
    observed.write_text(_application(inputs).model_dump_json(), encoding="utf-8")
    output = new_manifest if destination == "new_manifest" else new_proof
    before = _digest(output)
    ledger_before = _digest(inputs["ledger_path"])
    with pytest.raises(SystemExit) as failure:
        main(
            [
                "prepare",
                "--knowledge",
                str(knowledge_path),
                "--input",
                str(observed),
                "--output",
                str(output),
            ]
        )
    assert failure.value.code == 2
    assert _digest(output) == before
    assert _digest(inputs["ledger_path"]) == ledger_before
