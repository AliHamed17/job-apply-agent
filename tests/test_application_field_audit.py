import json

import pytest

from batch_apply.activity import ActivityStore
from batch_apply.field_audit import append_field_captures
from scripts.log_application_fields import main

URL = "https://example.com/jobs/42"


def field(event_id, label, entered, read_back, **kwargs):
    return {
        "event_id": event_id,
        "label": label,
        "action": "entered",
        "entered": entered,
        "read_back": read_back,
        "timestamp": "2026-11-21T12:00:00Z",
        **kwargs,
    }


def batch(fields):
    return {
        "run_id": "run-1",
        "job_url": URL,
        "company": "Example",
        "title": "Engineer",
        "fields": fields,
    }


def test_structured_fields_retain_readback_and_detect_mismatch(tmp_path):
    db = str(tmp_path / "ledger.db")
    payload = batch(
        [
            field("one", "Email", "a@example.com", "a@example.com", source="verified_profile"),
            field("two", "Address", "Main Street", "a@example.com"),
        ]
    )
    result = append_field_captures(db, payload)
    assert result["verification_counts"] == {"match": 1, "mismatch": 1}
    assert result["requires_attention"] is True
    records = ActivityStore(db).export(run_id="run-1")["events"]
    assert records[1]["observed"]["label"] == "Address"
    assert records[1]["entered"]["value"] == "Main Street"
    assert records[1]["details"]["read_back"]["value"] == "a@example.com"
    assert records[1]["details"]["historical_observation_not_verified_fact"] is True
    assert append_field_captures(db, payload)["duplicate_ids"] == 2


def test_unreadable_readback_requires_attention_and_bool_is_not_one(tmp_path):
    result = append_field_captures(
        str(tmp_path / "ledger.db"),
        batch([field("a", "Ready", True, 1), field("b", "Phone", "123", None)]),
    )
    assert result["verification_counts"] == {"mismatch": 1, "unverified": 1}
    assert result["requires_attention"] is True


@pytest.mark.parametrize("key", ["before", "entered", "read_back"])
def test_credential_values_rejected_atomically(tmp_path, key):
    db = str(tmp_path / "ledger.db")
    bad = field("secret", "Verification code", "", "")
    bad[key] = "sample-secret"
    with pytest.raises(ValueError):
        append_field_captures(db, batch([field("good", "Name", "A", "A"), bad]))
    assert not (tmp_path / "ledger.db").exists()


def test_cli_never_prints_field_values(tmp_path, capsys):
    p = tmp_path / "input.json"
    p.write_text(
        json.dumps(batch([field("one", "Email", "private@example.com", "private@example.com")]))
    )
    assert main([str(p), "--ledger", str(tmp_path / "ledger.db")]) == 0
    assert "private@example.com" not in capsys.readouterr().out
