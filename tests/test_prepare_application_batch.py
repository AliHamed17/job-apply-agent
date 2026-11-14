from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.prepare_application_batch as batch


def _inputs(tmp_path: Path, monkeypatch, applications: list[dict]) -> tuple[Path, Path]:
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime-data"
    runtime.mkdir()
    input_path = runtime / "observed-applications.json"
    input_path.write_text(json.dumps({"applications": applications}), encoding="utf-8")
    cv_directory = tmp_path / "cvs"
    cv_directory.mkdir()
    knowledge_path = runtime / "application-knowledge.json"
    knowledge_path.write_text(
        json.dumps(
            {
                "schema_version": "application-workflow-knowledge-v1",
                "ledger_path": str(tmp_path / "ledger.sqlite3"),
                "answer_cache_path": str(runtime / "answer-cache.json"),
                "cv_directory": str(cv_directory),
                "employer_policy_path": str(runtime / "employer-policy.json"),
                "protected_source_paths": [],
            }
        ),
        encoding="utf-8",
    )
    return input_path, knowledge_path


def _application(url: str = "https://example.test/jobs/42") -> dict:
    return {
        "job": {"url": url, "title": "Software Engineer", "company": "Example"},
        "fields": [{"field_id": "email", "label": "Email", "required": True}],
    }


def _packet(application) -> dict:
    return {
        "schema_version": "application-work-packet-v1",
        "generated_at": "2026-11-09T00:00:00+00:00",
        "job": {
            "url": application.job.url,
            "title": application.job.title,
            "company": application.job.company,
        },
        "next_action": "review_and_claim",
        "ledger_modified": False,
        "browser_actions_performed": False,
        "submission_ready": False,
        "requires_live_review": True,
        "execution_steps": [],
        "fields": [],
        "cv_candidate": None,
    }


def test_batch_prepares_unique_jobs_locally_and_records_zero_cost_calls(
    tmp_path, monkeypatch, capsys
):
    input_path, knowledge_path = _inputs(
        tmp_path,
        monkeypatch,
        [
            _application(),
            _application("https://example.test/jobs/42?utm_source=whatsapp"),
        ],
    )
    calls = []

    def prepare(application, **kwargs):
        calls.append(application.job.url)
        return _packet(application)

    monkeypatch.setattr(batch, "prepare_packet", prepare)
    out_dir = "runtime-data/prepared-runs/run-test"
    result = batch.main(
        [
            "--input",
            str(input_path),
            "--knowledge",
            str(knowledge_path),
            "--output-dir",
            out_dir,
        ]
    )

    assert result == 0
    assert len(calls) == 1
    summary = json.loads((tmp_path / out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["input_count"] == 2
    assert summary["unique_job_count"] == 1
    assert summary["action_counts"] == {"review_and_claim": 1, "duplicate_in_batch": 1}
    assert summary["llm_calls"] == summary["network_requests"] == 0
    assert summary["browser_actions"] == summary["ledger_mutations"] == 0
    assert len(list((tmp_path / out_dir).glob("*.json"))) == 2  # One packet and one summary.
    assert json.loads(capsys.readouterr().out)["status"] == "batch_prepared"


def test_batch_keeps_going_after_invalid_record_and_returns_failure_status(
    tmp_path, monkeypatch, capsys
):
    input_path, knowledge_path = _inputs(
        tmp_path,
        monkeypatch,
        [
            {"job": {"url": ""}},
            _application(),
        ],
    )
    monkeypatch.setattr(batch, "prepare_packet", lambda app, **kwargs: _packet(app))
    out_dir = "runtime-data/prepared-runs/run-partial"

    result = batch.main(
        [
            "--input",
            str(input_path),
            "--knowledge",
            str(knowledge_path),
            "--output-dir",
            out_dir,
        ]
    )

    assert result == 1
    summary = json.loads((tmp_path / out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["input_count"] == 2
    assert summary["unique_job_count"] == 1
    assert summary["action_counts"]["input_or_prepare_error"] == 1
    assert summary["action_counts"]["review_and_claim"] == 1
    assert json.loads(capsys.readouterr().out)["status"] == "batch_prepared_with_errors"


def test_batch_rejects_oversized_input_before_creating_output(tmp_path, monkeypatch):
    input_path, knowledge_path = _inputs(tmp_path, monkeypatch, [_application(), _application()])
    out_dir = "runtime-data/prepared-runs/should-not-exist"

    with pytest.raises(SystemExit) as error:
        batch.main(
            [
                "--input",
                str(input_path),
                "--knowledge",
                str(knowledge_path),
                "--output-dir",
                out_dir,
                "--max-applications",
                "1",
            ]
        )

    assert error.value.code == 2
    assert not (tmp_path / out_dir).exists()


def test_batch_rejects_output_outside_private_runtime_directory(tmp_path, monkeypatch):
    input_path, knowledge_path = _inputs(tmp_path, monkeypatch, [_application()])

    with pytest.raises(SystemExit) as error:
        batch.main(
            [
                "--input",
                str(input_path),
                "--knowledge",
                str(knowledge_path),
                "--output-dir",
                str(tmp_path / "public-output"),
            ]
        )

    assert error.value.code == 2
    assert not (tmp_path / "public-output").exists()


def test_batch_rejects_output_inside_cv_directory(tmp_path, monkeypatch):
    input_path, knowledge_path = _inputs(tmp_path, monkeypatch, [_application()])
    cv_directory = tmp_path / "runtime-data" / "cvs"
    cv_directory.mkdir()
    knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
    knowledge["cv_directory"] = str(cv_directory)
    knowledge_path.write_text(json.dumps(knowledge), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        batch.main(
            [
                "--input",
                str(input_path),
                "--knowledge",
                str(knowledge_path),
                "--output-dir",
                "runtime-data/cvs/prepared-run",
            ]
        )

    assert error.value.code == 2
    assert not (cv_directory / "prepared-run").exists()
