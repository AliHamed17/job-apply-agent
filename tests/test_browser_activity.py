"""Activity audit fidelity, durable append-only behavior, and secret exclusion."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from batch_apply.activity import ActivityStore
from batch_apply.ledger import BatchLedger
from batch_apply.models import BatchStatus, TabSnapshot
from scripts.log_browser_activity import main

URL = "https://www.linkedin.com/jobs/view/1234567890"


def _event(**changes):
    return {
        "event_id": "step-1",
        "job_url": URL,
        "company": "Example",
        "title": "Engineer",
        "event_type": "form_field",
        "timestamp": "2026-11-07T18:00:00+02:00",
        "observed": {"label": "First name", "value": ""},
        "entered": {"label": "First name", "value": "Avery"},
        **changes,
    }


def test_activity_releases_owned_connections_after_each_operation(tmp_path, monkeypatch):
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
    store = ActivityStore(tmp_path / "activity.sqlite3")
    store.append_batch({"run_id": "test-run", "events": [_event()]})
    store.export(run_id="test-run")
    released = bool(connections) and all(c.explicitly_closed for c in connections)
    for connection in connections:
        connection.close()
    assert released


def test_step_values_and_cv_artifact_provenance_survive_reopen_without_changing_outcome(tmp_path):
    database = tmp_path / "batch.sqlite3"
    ledger = BatchLedger(database)
    ledger.import_tabs([TabSnapshot(url=URL, title="Engineer")])
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF-1.4\nobserved existing CV")
    screenshot = tmp_path / "step.png"
    screenshot.write_bytes(b"image-fixture")
    store = ActivityStore(database)
    store.append_batch(
        {
            "run_id": "run-a",
            "events": [
                _event(),
                _event(
                    event_id="step-2",
                    event_type="upload",
                    observed="No CV attached",
                    entered="CV.pdf",
                    cv_path=str(cv),
                    artifact_paths=[str(screenshot)],
                ),
                _event(
                    event_id="step-3",
                    event_type="confirmation",
                    observed="Application submitted",
                    entered=None,
                ),
            ],
        }
    )
    result = ActivityStore(database).export(run_id="run-a", job_url=URL + "/?trk=jobs")
    assert result["count"] == 3
    first, upload, confirmation = result["events"]
    assert first["observed"]["value"] == ""
    assert first["entered"]["value"] == "Avery"
    assert first["timestamp"] == "2026-11-07T16:00:00Z"
    assert upload["cv_sha256"] == hashlib.sha256(cv.read_bytes()).hexdigest()
    assert upload["artifact_paths"] == [str(screenshot.resolve())]
    assert screenshot.read_bytes() not in database.read_bytes()
    assert confirmation["event_type"] == "confirmation"
    assert ledger.get(URL).status == BatchStatus.DISCOVERED


def test_explicit_event_retry_is_idempotent_and_changed_event_is_rejected_atomically(tmp_path):
    store = ActivityStore(tmp_path / "batch.sqlite3")
    payload = {"run_id": "run-a", "events": [_event()]}
    assert len(store.append_batch(payload)["inserted"]) == 1
    assert store.append_batch(payload)["duplicate_ids"] == ["step-1"]
    with pytest.raises(ValueError, match="cannot be changed"):
        store.append_batch(
            {
                "run_id": "run-a",
                "events": [
                    _event(event_id="step-2"),
                    _event(entered="different candidate value"),
                ],
            }
        )
    assert store.export()["count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"entered": {"password": "do-not-store"}},
        {"entered": {"label": "Verification code", "value": "123456"}},
        {"entered": {"label": "Enter the 6 digit code", "value": "123456"}},
        {"entered": {"label": "קוד אימות", "value": "123456"}},
        {"entered": {"recovery_answer": "do-not-store"}},
        {"observed": {"name": "session_token", "value": "do-not-store"}},
        {"details": {"headers": {"Cookie": "do-not-store"}}},
        {"details": {"authorization": "Bearer do-not-store"}},
        {"observed": "password=do-not-store"},
        {"observed": "https://example.com/verify?code=123456"},
        {"job_url": "https://example.com/jobs/1?access_token=do-not-store"},
        {"observed": "https://example.com/verify#session_token=do-not-store"},
    ],
)
def test_secret_event_rejects_entire_batch_without_persisting_secret(tmp_path, changes):
    database = tmp_path / "batch.sqlite3"
    store = ActivityStore(database)
    with pytest.raises(ValueError, match="prohibited") as error:
        store.append_batch(
            {
                "run_id": "run-a",
                "events": [
                    _event(event_id="safe-step"),
                    _event(**changes),
                ],
            }
        )
    assert "do-not-store" not in str(error.value)
    assert store.export()["count"] == 0
    assert b"do-not-store" not in database.read_bytes()


def test_local_pii_and_sanitized_auth_blocker_are_allowed(tmp_path):
    store = ActivityStore(tmp_path / "batch.sqlite3")
    store.append_batch(
        {
            "run_id": "run-a",
            "events": [
                _event(entered={"email": "ali@example.com", "phone": "+972500000000"}),
                _event(
                    event_id="auth-step",
                    event_type="blocker",
                    observed="Password field present",
                    entered=None,
                    details={"password_required": True},
                ),
            ],
        }
    )
    events = store.export()["events"]
    assert events[0]["entered"]["email"] == "ali@example.com"
    assert events[1]["details"]["password_required"] is True


def test_database_prevents_update_and_delete_and_cv_mismatch_adds_no_event(tmp_path):
    database = tmp_path / "batch.sqlite3"
    store = ActivityStore(database)
    store.append_batch({"run_id": "run-a", "events": [_event()]})
    with sqlite3.connect(database) as connection:
        for statement in (
            "UPDATE browser_batch_activity SET run_id='changed'",
            "DELETE FROM browser_batch_activity",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"new contents")
    with pytest.raises(ValueError, match="hash"):
        store.append_batch(
            {
                "run_id": "run-a",
                "events": [
                    _event(event_id="cv-step", cv_path=str(cv), cv_sha256="0" * 64),
                ],
            }
        )
    assert store.export()["count"] == 1


def test_cli_append_and_filtered_export_and_safe_validation_error(tmp_path, capsys):
    database = tmp_path / "batch.sqlite3"
    input_file = tmp_path / "events.json"
    export_file = tmp_path / "export.json"
    input_file.write_text(json.dumps({"run_id": "run-a", "events": [_event()]}))
    assert main(["append", str(input_file), "--ledger", str(database)]) == 0
    assert (
        main(
            ["export", "--ledger", str(database), "--run-id", "run-a", "--output", str(export_file)]
        )
        == 0
    )
    assert json.loads(export_file.read_text())["count"] == 1
    assert ActivityStore(database).export(run_id="different-run")["count"] == 0
    input_file.write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "events": [
                    _event(timestamp="not-a-timestamp", entered={"password": "do-not-store"}),
                ],
            }
        )
    )
    with pytest.raises(SystemExit) as error:
        main(["append", str(input_file), "--ledger", str(database)])
    assert error.value.code == 2
    assert "do-not-store" not in capsys.readouterr().err
