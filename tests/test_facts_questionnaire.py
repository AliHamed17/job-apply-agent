"""Questionnaire build/apply on synthetic ledgers; answers must satisfy the real answer cache."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from batch_apply.answer_cache import (
    build_answer_cache,
    load_answer_cache,
    lookup_answer,
    validate_cache_sources,
)
from batch_apply.facts_gap import experience_scope, mine_ledger
from batch_apply.models import BatchJob, BatchStatus
from batch_apply.urls import job_key
from scripts.facts_questionnaire import (
    apply_questionnaire,
    build_questionnaire,
    main,
    render_markdown,
)


def _job(n, status, reason, company="Acme Ltd"):
    url = f"https://www.comeet.com/jobs/acme/{n}/role/AA.{n}"
    return BatchJob(
        job_id=job_key(url),
        canonical_url=url,
        title="QA Engineer",
        company=company,
        status=status,
        reason=reason,
    )


@pytest.fixture
def ledger(tmp_path):
    jobs = [
        _job(1, BatchStatus.SKIPPED, "Required exact SQL years are unknown.", "Alpha"),
        _job(2, BatchStatus.SKIPPED, "Required XML and SQL experience durations are not verified."),
        _job(3, BatchStatus.BLOCKED, "Required checkbox accepts Terms of Service and Privacy."),
        _job(4, BatchStatus.SKIPPED, "Required salary expectations are unknown."),
        _job(5, BatchStatus.SKIPPED, "Required notice period is unknown."),
        _job(6, BatchStatus.BLOCKED, "Form needs reCAPTCHA before Submit."),
        _job(7, BatchStatus.SKIPPED, "Requires a B.Sc. in Electrical Engineering."),
        _job(8, BatchStatus.SUBMITTED, "ok"),
    ]
    path = tmp_path / "ledger.sqlite3"
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
        connection.commit()
    return path


def test_build_questionnaire_shape_and_privacy(ledger):
    questionnaire = build_questionnaire(mine_ledger(ledger))
    assert questionnaire["schema_version"] == "facts-questionnaire-v1"
    summary = questionnaire["summary"]
    assert summary["blocked_or_skipped_jobs"] == 7
    # SQL, XML, consent (terms), salary and notice jobs are answerable; captcha and degree are not.
    assert summary["unblocked_if_all_answered"] == 5
    skills = {item["skill"]: item for item in questionnaire["experience"]}
    assert skills["SQL"]["jobs"] == 2
    assert skills["SQL"]["years"] is None
    assert skills["SQL"]["scope"] == experience_scope("sql")
    assert {item["key"] for item in questionnaire["preferences"]} == {
        "salary.expectation",
        "availability.notice_or_start",
    }
    assert all(item["answer"] is None for item in questionnaire["preferences"])
    assert questionnaire["consent_policy"]["answer"] is None
    assert {item["key"] for item in questionnaire["not_fixable_by_facts"]} == {
        "obstacle.captcha",
        "fit.education_requirement",
    }
    # Employer names stay private but URLs and job titles must never be copied out.
    dumped = json.dumps(questionnaire)
    assert "http" not in dumped
    assert "QA Engineer" not in dumped


def test_markdown_lists_each_section(ledger):
    text = render_markdown(build_questionnaire(mine_ledger(ledger)))
    assert "| SQL | 2 |" in text
    assert "Salary expectation" in text
    assert "Consent checkboxes (1 job)" in text
    assert "CAPTCHA or human check" in text


def _filled(tmp_path, **changes):
    questionnaire = {
        "schema_version": "facts-questionnaire-v1",
        "experience": [{"key": "experience.sql_years", "years": 3}],
        "preferences": [{"key": "salary.expectation", "answer": "negotiable"}],
        "consent_policy": {"answer": "accept_required_terms_and_privacy_only"},
    }
    questionnaire.update(changes)
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(questionnaire), encoding="utf-8")
    return path


def test_apply_writes_reviewed_facts_the_real_answer_cache_accepts(tmp_path, ledger):
    paths = apply_questionnaire(_filled(tmp_path), tmp_path / "out")
    manifest = json.loads(paths["reviewed_facts"].read_text(encoding="utf-8"))
    (fact,) = manifest["facts"]
    assert fact["scope"] == experience_scope("sql")
    assert fact["source_type"] == "user_confirmation"
    assert fact["reviewed"] is True
    assert fact["inherited_prefill"] is False
    proof = paths["proof"]
    assert fact["proofs"] == [
        {"path": str(proof.resolve()), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
    ]

    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"provenance": {}}), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text("personal:\n  location: Haifa\n", encoding="utf-8")
    cvs = tmp_path / "cvs"
    cvs.mkdir()
    cache = build_answer_cache(
        ledger_path=ledger,
        overlay_path=overlay,
        profile_path=profile,
        cv_directory=cvs,
        verified_facts_path=paths["reviewed_facts"],
    )
    found = lookup_answer(cache, "experience.sql_years", scope=experience_scope("sql"))
    assert found is not None
    assert found.value == 3
    assert lookup_answer(cache, "experience.sql_years", scope="some other question") is None
    assert lookup_answer(cache, "experience.python_years", scope=experience_scope("python")) is None

    saved = tmp_path / "cache.json"
    saved.write_text(cache.model_dump_json(), encoding="utf-8")
    reloaded = load_answer_cache(saved)
    assert (
        lookup_answer(reloaded, "experience.sql_years", scope=experience_scope("sql")) is not None
    )
    proof.write_text(proof.read_text(encoding="utf-8") + " ", encoding="utf-8")  # tamper
    tampered = load_answer_cache(saved)
    assert validate_cache_sources(tampered) == ["cache_source_changed"]
    # A changed proof disables every cached answer instead of silently serving a stale one.
    assert lookup_answer(tampered, "experience.sql_years", scope=experience_scope("sql")) is None


def test_apply_stores_preferences_separately_and_marks_them_unused(tmp_path):
    paths = apply_questionnaire(_filled(tmp_path), tmp_path / "out")
    stored = json.loads(paths["preferences"].read_text(encoding="utf-8"))
    assert stored["used_by_answer_cache"] is False
    assert stored["answers"] == {
        "salary.expectation": "negotiable",
        "consent.terms_or_privacy": "accept_required_terms_and_privacy_only",
    }
    reviewed = json.loads(paths["reviewed_facts"].read_text(encoding="utf-8"))
    assert all(f["key"].startswith("experience.") for f in reviewed["facts"])


def test_blank_answers_are_not_written(tmp_path):
    path = _filled(
        tmp_path,
        experience=[{"key": "experience.sql_years", "years": None}],
        preferences=[{"key": "salary.expectation", "answer": "negotiable"}],
        consent_policy={"answer": None},
    )
    paths = apply_questionnaire(path, tmp_path / "out")
    assert "reviewed_facts" not in paths
    assert "preferences" in paths


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "other"},
        {"experience": [{"key": "experience.sql_years", "years": True}]},
        {"experience": [{"key": "experience.sql_years", "years": "3"}]},
        {"experience": [{"key": "experience.sql_years", "years": -1}]},
        {"experience": [{"key": "experience.sql_years", "years": 81}]},
        {"experience": [{"key": "salary.expectation", "years": 3}]},
        {"experience": [{"key": "experience.Sql_years", "years": 3}]},
        {
            "experience": [
                {"key": "experience.sql_years", "years": 3},
                {"key": "experience.sql_years", "years": 4},
            ]
        },
        {"preferences": [{"key": "salary.expectation", "answer": "  "}]},
        {"preferences": [{"key": "salary.expectation", "answer": "x" * 201}]},
        {"preferences": [{"key": "address.postal_code", "answer": "ID 123456789012"}]},
        {"consent_policy": {"answer": "accept_everything"}},
        {"experience": [], "preferences": [], "consent_policy": {}},
    ],
)
def test_apply_rejects_invalid_answers(tmp_path, changes):
    with pytest.raises(ValueError):
        apply_questionnaire(_filled(tmp_path, **changes), tmp_path / "out")
    assert not (tmp_path / "out" / "reviewed-facts.json").exists()


def test_apply_rejects_credentials_in_the_file(tmp_path):
    path = _filled(tmp_path, preferences=[{"key": "salary.expectation", "password": "hunter2"}])
    with pytest.raises(ValueError):
        apply_questionnaire(path, tmp_path / "out")


def test_apply_accepts_user_added_skill_and_decimal_years(tmp_path):
    path = _filled(tmp_path, experience=[{"key": "experience.rust_years", "years": 1.5}])
    paths = apply_questionnaire(path, tmp_path / "out")
    (fact,) = json.loads(paths["reviewed_facts"].read_text(encoding="utf-8"))["facts"]
    assert fact["value"] == 1.5
    assert fact["scope"] == "years of professional experience with rust"


def test_apply_keeps_each_proof_and_regenerates_facts(tmp_path):
    out = tmp_path / "out"
    first = apply_questionnaire(_filled(tmp_path), out)
    same = apply_questionnaire(_filled(tmp_path), out)
    assert first["proof"] == same["proof"]
    changed = apply_questionnaire(
        _filled(tmp_path, experience=[{"key": "experience.sql_years", "years": 4}]), out
    )
    assert changed["proof"] != first["proof"]
    assert first["proof"].exists()  # the earlier proof stays valid for any cache built from it
    (fact,) = json.loads(changed["reviewed_facts"].read_text(encoding="utf-8"))["facts"]
    assert fact["value"] == 4


def test_cli_build_keeps_existing_answers_and_apply_prints_paths(tmp_path, ledger, capsys):
    out = tmp_path / "facts"
    assert main(["build", "--ledger", str(ledger), "--out-dir", str(out)]) == 0
    original = (out / "facts-questionnaire.json").read_text(encoding="utf-8")
    (out / "facts-questionnaire.json").write_text(original + "\n", encoding="utf-8")  # user edit
    assert main(["build", "--ledger", str(ledger), "--out-dir", str(out)]) == 0
    assert (out / "facts-questionnaire.json").read_text(encoding="utf-8") == original + "\n"
    assert len(list(out.glob("facts-questionnaire.2*.json"))) == 1
    assert (out / "facts-questionnaire.md").exists()

    answers = json.loads(original)
    answers["experience"][0]["years"] = 2
    (out / "facts-questionnaire.json").write_text(json.dumps(answers), encoding="utf-8")
    capsys.readouterr()
    assert (
        main(
            [
                "apply",
                "--questionnaire",
                str(out / "facts-questionnaire.json"),
                "--out-dir",
                str(out),
            ]
        )
        == 0
    )
    assert "reviewed_facts=" in capsys.readouterr().out


def test_cli_report_and_errors(tmp_path, ledger, capsys):
    assert main(["report", "--ledger", str(ledger), "--examples"]) == 0
    out = capsys.readouterr().out
    assert "blocked_or_skipped=7" in out
    assert "unblocked_if_all_fixable_answered=5" in out
    assert "obstacle.captcha" in out
    assert main(["build", "--ledger", str(tmp_path / "missing.sqlite3")]) == 2
    assert "error:" in capsys.readouterr().err
    assert main(["apply", "--questionnaire", str(tmp_path / "missing.json")]) == 2
