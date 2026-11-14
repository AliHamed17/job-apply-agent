"""Synthetic evidence never promotes form prefills into reusable user facts."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

import batch_apply.answer_cache as module
from batch_apply.answer_cache import (
    AnswerCache,
    build_answer_cache,
    load_answer_cache,
    lookup_answer,
    validate_cache_sources,
)
from batch_apply.catalog import choose_cv_file
from batch_apply.models import BatchJob, BatchStatus
from batch_apply.urls import job_key
from scripts.export_verified_answer_cache import main


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    directory = tmp_path / "cvs"
    directory.mkdir()
    cv = directory / "Data_Engineer_CV.pdf"
    cv.write_bytes(b"%PDF-1.4\nsynthetic PDF fixture")
    choice = choose_cv_file(
        directory, cv.name, source="llm_browser_review", reason="Reviewed synthetic role"
    )
    url = "https://employer.test/jobs/engineer"
    job = BatchJob(
        job_id=job_key(url),
        canonical_url=url,
        title="Data Engineer",
        company="Example Employer",
        status=BatchStatus.BLOCKED,
        selected_cv=choice,
        reason="Required exact Python years and salary are unknown",
    )
    database = tmp_path / "ledger.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE browser_batch_jobs(payload TEXT,canonical_url TEXT);"
            "CREATE TABLE browser_batch_activity(sequence INTEGER PRIMARY KEY,payload TEXT,"
            "event_type TEXT);"
        )
        connection.execute(
            "INSERT INTO browser_batch_jobs VALUES (?,?)", (job.model_dump_json(), url)
        )
        for typ in ["form_field", "upload", "blocker"]:
            event = {
                "event_id": typ,
                "event_type": typ,
                "job_id": job.job_id,
                "job_url": url,
                "run_id": "synthetic-run",
                "cv_sha256": choice.sha256,
                "entered": {"python_years_prefill": 9, "password": "fixture-only-secret"},
                "details": {"reason": job.reason},
            }
            connection.execute(
                "INSERT INTO browser_batch_activity(payload,event_type) VALUES (?,?)",
                (json.dumps(event), typ),
            )
        connection.commit()
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {
                "personal": {
                    "name": "Test Applicant",
                    "email": "person@test.invalid",
                    "phone": "+15551234567",
                    "national_id": "not-imported",
                },
                "links": {"linkedin": "https://www.linkedin.com/in/test-applicant/"},
                "provenance": {"personal": "Reviewed contact", "linkedin_github": "Reviewed link"},
                "credentials": {"password": "not-copied"},
            }
        ),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "personal:\n  location: Test Town, Region\n"
        "attachments:\n  - path: absent-academic-document.pdf\n"
        "links:\n  portfolio: https://example.com/jane-doe\n",
        encoding="utf-8",
    )
    video, frame = tmp_path / "source.bin", tmp_path / "review.bin"
    video.write_bytes(b"synthetic original evidence")
    frame.write_bytes(b"synthetic reviewed active entry")
    manifest = tmp_path / "reviewed-facts.json"
    manifest.write_text(
        json.dumps(
            {
                "facts": [
                    {
                        "id": "query-language",
                        "key": "experience.query_language_years",
                        "value": 2,
                        "scope": "exact reviewed query-language question",
                        "reviewed": True,
                        "inherited_prefill": False,
                        "source_type": "recording_active_answer",
                        "active_entry_observed": True,
                        "proofs": [{"path": str(p), "sha256": digest(p)} for p in (video, frame)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # Hash/provenance tests use a synthetic PDF; the real page-count guard has its own test.
    monkeypatch.setattr(module, "_one_page", lambda _: True)
    return {
        "ledger_path": database,
        "overlay_path": overlay,
        "profile_path": profile,
        "cv_directory": directory,
        "verified_facts_path": manifest,
    }


def test_verified_sources_are_scoped_and_prefills_remain_unknown(inputs):
    before = digest(inputs["ledger_path"])
    cache = build_answer_cache(**inputs)
    assert digest(inputs["ledger_path"]) == before
    assert (
        lookup_answer(
            cache, "experience.query_language_years", scope="exact reviewed query-language question"
        ).value
        == 2
    )
    assert lookup_answer(cache, "experience.query_language_years", scope="other question") is None
    assert lookup_answer(cache, "experience.python_years", scope="python years") is None
    assert lookup_answer(cache, "location.configured_city", scope="daily commute") is None
    assert set(cache.unknowns) == {"experience.python_years", "salary.expectation"}
    assert cache.cv_routes[0]["examples"][0]["upload_event_ids"] == ["upload"]
    rendered = cache.model_dump_json()
    for secret in ["fixture-only-secret", "not-copied", "national_id", "absent-academic"]:
        assert secret not in rendered


def test_loaded_cache_rechecks_sources_and_answer_values(inputs, tmp_path):
    cache = build_answer_cache(**inputs)
    path = tmp_path / "cache.json"
    path.write_text(cache.model_dump_json(), encoding="utf-8")
    loaded = load_answer_cache(path)
    answer = lookup_answer(loaded, "personal.name", scope="ordinary_job_contact")
    assert answer.value == "Test Applicant"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["facts"][0]["value"] = "Different Applicant"
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_answer_cache(path)
    assert lookup_answer(loaded, "personal.name", scope="ordinary_job_contact") is None


@pytest.mark.parametrize("source", ["overlay_path", "profile_path", "verified_facts_path"])
def test_changed_source_disables_loaded_answers(inputs, source):
    cache = build_answer_cache(**inputs)
    Path(inputs[source]).write_text("changed", encoding="utf-8")
    assert validate_cache_sources(cache)
    assert lookup_answer(cache, "personal.name", scope="ordinary_job_contact") is None


def test_serialized_cache_is_not_trusted_until_verified(inputs):
    cache = AnswerCache.model_validate_json(build_answer_cache(**inputs).model_dump_json())
    assert lookup_answer(cache, "personal.name", scope="ordinary_job_contact") is None
    assert validate_cache_sources(cache) == []
    assert lookup_answer(cache, "personal.name", scope="ordinary_job_contact") is not None


@pytest.mark.parametrize(
    "change",
    [
        "prefill",
        "not_reviewed",
        "no_active_entry",
        "wrong_hash",
        "single_proof",
        "boolean_years",
        "duplicate_key",
        "identity_key",
    ],
)
def test_unreviewed_or_unbound_duration_cannot_be_cached(inputs, change):
    path = inputs["verified_facts_path"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    fact = raw["facts"][0]
    if change == "prefill":
        fact["inherited_prefill"] = True
    elif change == "not_reviewed":
        fact["reviewed"] = False
    elif change == "no_active_entry":
        fact["active_entry_observed"] = False
    elif change == "wrong_hash":
        fact["proofs"][0]["sha256"] = "0" * 64
    elif change == "single_proof":
        fact["proofs"] = fact["proofs"][:1]
    elif change == "boolean_years":
        fact["value"] = True
    elif change == "duplicate_key":
        raw["facts"].append({**fact, "id": "another-id"})
    else:
        fact["key"] = "personal.national_id"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        build_answer_cache(**inputs)


def test_missing_recording_proof_cannot_validate_serialized_cache(inputs):
    raw = build_answer_cache(**inputs).model_dump(mode="json")
    raw["source_files"] = [
        item for item in raw["source_files"] if not item["path"].endswith("review.bin")
    ]
    cache = AnswerCache.model_validate(raw)
    assert "reviewed_proof_not_bound" in validate_cache_sources(cache)


def test_changed_cv_is_not_reused(inputs):
    cv = inputs["cv_directory"] / "Data_Engineer_CV.pdf"
    cache = build_answer_cache(**inputs)
    cv.write_bytes(b"%PDF-1.4\nchanged PDF")
    assert validate_cache_sources(cache)
    assert build_answer_cache(**inputs).cv_routes == []


def test_filename_suggestion_is_not_a_reviewed_cv_route(inputs):
    with closing(sqlite3.connect(inputs["ledger_path"])) as connection:
        raw = json.loads(connection.execute("SELECT payload FROM browser_batch_jobs").fetchone()[0])
        raw["selected_cv"]["source"] = "filename_role_match"
        connection.execute("UPDATE browser_batch_jobs SET payload=?", (json.dumps(raw),))
        connection.commit()
    assert build_answer_cache(**inputs).cv_routes == []


def test_repeated_cv_routes_are_page_checked_once(inputs, monkeypatch):
    calls = []
    monkeypatch.setattr(module, "_one_page", lambda cv: calls.append(cv.file) or True)
    with closing(sqlite3.connect(inputs["ledger_path"])) as connection:
        raw = json.loads(connection.execute("SELECT payload FROM browser_batch_jobs").fetchone()[0])
        raw["canonical_url"] = "https://employer.test/jobs/another-engineer"
        raw["job_id"] = job_key(raw["canonical_url"])
        connection.execute(
            "INSERT INTO browser_batch_jobs VALUES (?,?)", (json.dumps(raw), raw["canonical_url"])
        )
        connection.commit()
    cache = build_answer_cache(**inputs)
    assert len(calls) == 1 and cache.cv_routes[0]["job_count"] == 2


def test_unbound_cv_route_disables_cache(inputs):
    raw = build_answer_cache(**inputs).model_dump(mode="json")
    raw["cv_routes"][0]["file"] = "Different_Resume.pdf"
    cache = AnswerCache.model_validate(raw)
    assert "cv_route_not_bound" in validate_cache_sources(cache)


def test_unreviewed_overlay_fields_and_stale_profile_links_are_ignored(inputs):
    overlay = json.loads(inputs["overlay_path"].read_text(encoding="utf-8"))
    overlay["provenance"] = {}
    inputs["overlay_path"].write_text(json.dumps(overlay), encoding="utf-8")
    cache = build_answer_cache(**inputs)
    assert not any(fact.key.startswith(("personal.", "links.")) for fact in cache.facts)


def test_unsafe_professional_link_is_not_cached(inputs):
    overlay = json.loads(inputs["overlay_path"].read_text(encoding="utf-8"))
    overlay["links"]["linkedin"] += "?token=fixture-secret"
    inputs["overlay_path"].write_text(json.dumps(overlay), encoding="utf-8")
    assert not any(fact.key == "links.linkedin" for fact in build_answer_cache(**inputs).facts)


def test_cache_cannot_inject_unsupported_fact_key(inputs):
    raw = build_answer_cache(**inputs).model_dump(mode="json")
    raw["facts"][0]["key"] = "personal.government_identifier"
    with pytest.raises(ValidationError):
        AnswerCache.model_validate(raw)


def test_one_page_guard_without_text_extraction(tmp_path, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    for pages in (1, 2):
        path = tmp_path / f"resume-{pages}.pdf"
        writer = pypdf.PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=100, height=100)
        writer.write(path)
        choice = choose_cv_file(tmp_path, path.name, source="llm_browser_review", reason="Test")
        monkeypatch.setattr(
            pypdf._page.PageObject,
            "extract_text",
            lambda *_: pytest.fail("CV content must not be extracted"),
        )
        assert module._one_page(choice) is (pages == 1)


def test_cli_prints_only_counts_and_cannot_overwrite_input(inputs, tmp_path, capsys):
    output = tmp_path / "private-cache.json"
    args = [
        "--ledger",
        str(inputs["ledger_path"]),
        "--overlay",
        str(inputs["overlay_path"]),
        "--profile",
        str(inputs["profile_path"]),
        "--cv-dir",
        str(inputs["cv_directory"]),
        "--verified-facts",
        str(inputs["verified_facts_path"]),
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert output.exists()
    printed = capsys.readouterr().out
    assert "person@test.invalid" not in printed and "+15551234567" not in printed
    before = digest(inputs["overlay_path"])
    args[-1] = str(inputs["overlay_path"])
    with pytest.raises(SystemExit):
        main(args)
    assert digest(inputs["overlay_path"]) == before


def test_malformed_private_yaml_is_reported_without_echoing_source(inputs, tmp_path, capsys):
    sentinel = "PRIVATE_YAML_SENTINEL"
    inputs["profile_path"].write_text(f"personal:\n  location: [{sentinel}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--ledger",
                str(inputs["ledger_path"]),
                "--overlay",
                str(inputs["overlay_path"]),
                "--profile",
                str(inputs["profile_path"]),
                "--cv-dir",
                str(inputs["cv_directory"]),
                "--verified-facts",
                str(inputs["verified_facts_path"]),
                "--output",
                str(tmp_path / "cache.json"),
            ]
        )
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert sentinel not in captured.err + captured.out
    assert "no ledger changes" in captured.err


def test_malformed_bound_yaml_disables_loaded_answers_without_parser_exception(inputs):
    cache = build_answer_cache(**inputs)
    profile = inputs["profile_path"]
    profile.write_text("personal:\n  location: [PRIVATE_YAML_SENTINEL\n", encoding="utf-8")
    cache.source_files = [
        source.model_copy(update={"sha256": digest(profile), "size_bytes": profile.stat().st_size})
        if source.path == str(profile.resolve())
        else source
        for source in cache.source_files
    ]
    assert "answer_provenance_invalid" in validate_cache_sources(cache)
    assert lookup_answer(cache, "location.configured_city", scope="ordinary_job_city") is None


def test_cli_cannot_overwrite_a_bound_evidence_proof(inputs, tmp_path, capsys):
    proof = tmp_path / "reviewed-proof.json"
    proof.write_text('{"observed":"PRIVATE_PROOF_SENTINEL"}', encoding="utf-8")
    manifest = json.loads(inputs["verified_facts_path"].read_text(encoding="utf-8"))
    manifest["facts"][0]["proofs"].append({"path": str(proof), "sha256": digest(proof)})
    inputs["verified_facts_path"].write_text(json.dumps(manifest), encoding="utf-8")
    before = digest(proof)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--ledger",
                str(inputs["ledger_path"]),
                "--overlay",
                str(inputs["overlay_path"]),
                "--profile",
                str(inputs["profile_path"]),
                "--cv-dir",
                str(inputs["cv_directory"]),
                "--verified-facts",
                str(inputs["verified_facts_path"]),
                "--output",
                str(proof),
            ]
        )
    assert error.value.code == 2
    assert digest(proof) == before
    captured = capsys.readouterr()
    assert "PRIVATE_PROOF_SENTINEL" not in captured.err + captured.out
