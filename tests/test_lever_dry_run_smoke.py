"""Guard contracts for the one-URL Lever dry-run smoke command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.lever_dry_run_smoke import _write_inspection_report
from submitters.browser_trace import RedactedTrace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lever_dry_run_smoke.py"
URL = "https://jobs.lever.co/example-company/11111111-2222-4333-8444-555555555555/apply"
SECRET = "lever-smoke-operator-secret-" + "x" * 32


def _run_smoke(
    tmp_path: Path,
    *,
    url: str = URL,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "SECRET_KEY": SECRET,
            "JOB_AGENT_OPERATOR_TOKEN": SECRET,
            "DRY_RUN": "true",
            "DRAFT_ONLY": "true",
            "PORTAL_FINAL_SUBMIT_ENABLED": "false",
            **overrides,
        }
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--url", url, "--report", str(tmp_path / "report.json")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_smoke_rejects_non_lever_url(tmp_path: Path) -> None:
    result = _run_smoke(tmp_path, url="https://www.linkedin.com/jobs/view/1")

    assert result.returncode == 2
    assert "Exactly one explicit HTTPS Lever application URL" in result.stderr


def test_smoke_requires_dry_run(tmp_path: Path) -> None:
    result = _run_smoke(tmp_path, DRY_RUN="false")

    assert result.returncode == 2
    assert "DRY_RUN=true is required" in result.stderr


def test_smoke_requires_operator_authentication(tmp_path: Path) -> None:
    result = _run_smoke(tmp_path, JOB_AGENT_OPERATOR_TOKEN="wrong-token")

    assert result.returncode == 2
    assert "operator authentication failed" in result.stderr


def test_smoke_rejects_final_submission_mode(tmp_path: Path) -> None:
    result = _run_smoke(tmp_path, PORTAL_FINAL_SUBMIT_ENABLED="true")

    assert result.returncode == 2
    assert "final submission must remain disabled" in result.stderr


def test_inspection_report_is_redacted_and_never_authorizes_submission(tmp_path: Path) -> None:
    trace = RedactedTrace(selector_version="lever-candidate-v5")
    trace.record("form_observed", field_types=["email", "file"], step=2)
    report_path = tmp_path / "inspection.json"

    _write_inspection_report(report_path, trace, qualified=True)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "lever-selector-inspection-v1"
    assert report["mode"] == "read_only_form_inspection"
    assert report["final_action_performed"] is False
    assert report["cv_uploaded"] is False
    assert "jobs.lever.co" not in report_path.read_text(encoding="utf-8")
