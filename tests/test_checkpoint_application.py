import json

import pytest

from scripts.checkpoint_application import guard, load_config, review_activity


def complete():
    job = {"status": "submitted", "selected_cv": {"sha256": "test-digest"},
           "latest_evidence": {"artifact": "receipt.txt"}}
    events = [
        {"event_type": "observation"},
        {"event_type": "form_field", "entered": {"label": "Example field", "value": "Example answer"}},
        {"event_type": "upload", "cv_sha256": "test-digest"},
        {"event_type": "observation", "details": {"reviewed_fields": ["Example field"]}},
        {"event_type": "submit_attempt"},
        {"event_type": "confirmation"},
    ]
    return job, events


def test_complete_observed_flow():
    assert review_activity(*complete()) == []


@pytest.mark.parametrize("kind", ["form_field", "upload", "submit_attempt", "confirmation"])
def test_missing_required_evidence_refuses_checkpoint(kind):
    job, events = complete()
    with pytest.raises(ValueError):
        review_activity(job, [e for e in events if e["event_type"] != kind])


def test_unrecorded_reviewed_answer_refuses_checkpoint():
    job, events = complete()
    events[3]["details"]["reviewed_fields"].append("Another question")
    with pytest.raises(ValueError, match="unrecorded_reviewed_field"):
        review_activity(job, events)


def test_historical_gaps_are_preserved_without_claiming_completeness():
    job, _ = complete()
    assert "no_step_records" in review_activity(job, [], historical=True)


def test_wrong_cv_fingerprint_refuses_checkpoint():
    job, events = complete()
    events[2]["cv_sha256"] = "different"
    with pytest.raises(ValueError, match="matching_cv_upload"):
        review_activity(job, events)


def test_confirmation_before_submit_refuses_checkpoint():
    job, events = complete()
    events[-2:] = reversed(events[-2:])
    with pytest.raises(ValueError, match="confirmation_before_submit"):
        review_activity(job, events)


def test_missing_receipt_refuses_checkpoint():
    job, events = complete()
    job["latest_evidence"] = {}
    with pytest.raises(ValueError, match="receipt_artifact"):
        review_activity(job, events)


def test_guard_refuses_unverified_push_and_later_outcome(tmp_path):
    cfg = {"private_dir": tmp_path / "private", "run_dir": tmp_path / "run"}
    for path in cfg.values():
        path.mkdir()
    (cfg["run_dir"] / "scope.json").write_text(json.dumps({"outcomes": [{}]}))
    path = cfg["private_dir"] / "sync-state.json"
    path.write_text(json.dumps({"pending": True}))
    with pytest.raises(ValueError, match="not remotely verified"):
        guard(cfg)
    path.write_text(json.dumps({"pending": False, "outcome_count": 0, "verified_commit": "abc"}))
    with pytest.raises(ValueError, match="not remotely checkpointed"):
        guard(cfg)
    path.write_text(json.dumps({"pending": False, "outcome_count": 1, "verified_commit": "abc"}))
    assert guard(cfg)["allowed"] is True


def test_key_inside_checkout_is_rejected(tmp_path):
    source = tmp_path / "source"
    data = {"source_root": str(source), "run_dir": str(source / "runtime-data/run"),
            "archive_worktree": str(tmp_path / "public"), "key_file": str(source / "secret.key"),
            "private_dir": str(source / "runtime-data/private"), "branch": "codex/test"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Key must be outside"):
        load_config(path)
