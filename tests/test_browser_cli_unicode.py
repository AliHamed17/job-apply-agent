"""Successful mutations remain successful with Windows legacy stdout codecs."""

from __future__ import annotations

import io
import json
import sys

import pytest

from batch_apply.activity import ActivityStore
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus
from scripts.log_browser_activity import main as activity_main
from scripts.plan_browser_batch import _write
from scripts.plan_browser_batch import main as plan_main

TITLE = "Software Engineer \uf104 \U0001f4c4 מועמד"
URL = "https://example.com/jobs/123"


def _capture_call(monkeypatch, encoding, action):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, errors="strict", newline="\n")
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", stream)
        result = action()
        stream.flush()
        output = buffer.getvalue().decode(encoding)
    stream.detach()
    return result, json.loads(output)


@pytest.mark.parametrize("encoding", ["ascii", "cp1255", "utf-8"])
def test_plan_and_claim_exit_successfully_after_mutation_with_unicode(
    tmp_path,
    monkeypatch,
    encoding,
):
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    (cv_dir / "Software_Engineer_CV.pdf").write_bytes(b"%PDF-1.4\nfixture")
    snapshot = tmp_path / "tabs.json"
    snapshot.write_text(json.dumps({"tabs": [{"url": URL, "title": TITLE}]}), encoding="utf-8")
    ledger_path = tmp_path / "batch.sqlite3"
    export_path = tmp_path / "plan.json"
    result, plan = _capture_call(
        monkeypatch,
        encoding,
        lambda: plan_main(
            [
                "plan",
                str(snapshot),
                "--cv-dir",
                str(cv_dir),
                "--ledger",
                str(ledger_path),
                "--output",
                str(export_path),
            ]
        ),
    )
    assert result == 0
    assert plan["jobs"][0]["title"] == TITLE
    assert BatchLedger(ledger_path).get(URL).status == BatchStatus.PREPARED
    assert TITLE in export_path.read_text(encoding="utf-8")
    result, claimed = _capture_call(
        monkeypatch,
        encoding,
        lambda: plan_main(
            [
                "claim",
                "--ledger",
                str(ledger_path),
            ]
        ),
    )
    assert result == 0
    assert claimed["job"]["title"] == TITLE
    assert BatchLedger(ledger_path).get(URL).status == BatchStatus.IN_PROGRESS


@pytest.mark.parametrize("encoding", ["ascii", "cp1255"])
def test_activity_append_and_export_keep_unicode_and_accurate_success(
    tmp_path,
    monkeypatch,
    encoding,
):
    ledger_path = tmp_path / "batch.sqlite3"
    source = tmp_path / "events.json"
    run_id = "run-\uf104-מועמד"
    source.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "events": [
                    {
                        "event_id": "field-\uf104",
                        "job_url": URL,
                        "title": TITLE,
                        "event_type": "observation",
                        "observed": {"document_text": "Role \uf104 details"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result, appended = _capture_call(
        monkeypatch,
        encoding,
        lambda: activity_main(
            [
                "append",
                str(source),
                "--ledger",
                str(ledger_path),
            ]
        ),
    )
    assert result == 0
    assert appended["run_id"] == run_id
    assert ActivityStore(ledger_path).export()["count"] == 1
    export_path = tmp_path / "activity.json"
    result, exported = _capture_call(
        monkeypatch,
        encoding,
        lambda: activity_main(
            [
                "export",
                "--ledger",
                str(ledger_path),
                "--output",
                str(export_path),
            ]
        ),
    )
    assert result == 0
    assert exported["events"][0]["title"] == TITLE
    assert TITLE in export_path.read_text(encoding="utf-8")


def test_host_stream_without_encoding_gets_lossless_json_fallback(monkeypatch):
    class AsciiHostStream:
        def __init__(self):
            self.parts = []

        def write(self, text):
            text.encode("ascii")
            self.parts.append(text)
            return len(text)

    stream = AsciiHostStream()
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", stream)
        _write({"title": TITLE}, None)
    assert json.loads("".join(stream.parts))["title"] == TITLE
