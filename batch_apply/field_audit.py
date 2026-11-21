"""Persist per-field application observations with explicit read-back checks.

This module records what a browser executor supplies; it does not inspect the
browser, establish that an answer is true, or submit an application.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from batch_apply.activity import ActivityStore, reject_secrets

FieldValue = StrictStr | StrictBool | StrictInt | StrictFloat | None


class FieldCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=500)
    control: str = Field(default="", max_length=100)
    step: str = Field(default="", max_length=200)
    required: bool = False
    action: Literal["entered", "observed_existing", "left_blank"]
    before: FieldValue = None
    entered: FieldValue = None
    read_back: FieldValue = None
    expected: FieldValue = None
    source: str = Field(default="", max_length=300)
    timestamp: datetime
    artifact_paths: list[str] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Field timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_capture(self):
        if self.action == "entered" and self.entered is None:
            raise ValueError("Entered action requires the value entered")
        if self.action != "entered" and self.entered is not None:
            raise ValueError("Only an entered action may contain an entered value")
        # Validate each value with its label: the generic activity secret filter
        # must also see read-back and prefill values next to the field name.
        for value in (self.before, self.entered, self.read_back, self.expected):
            reject_secrets({"label": self.label, "value": value})
        return self


class FieldCaptureBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=200)
    job_url: str = Field(min_length=1)
    company: str = ""
    title: str = ""
    fields: list[FieldCapture] = Field(min_length=1)


def _verification(field: FieldCapture) -> str:
    if field.action == "left_blank":
        if field.required or field.read_back is None:
            return "unverified"
        return "match" if field.read_back == "" else "mismatch"
    if field.action == "observed_existing":
        if field.read_back is None or field.expected is None:
            return "unverified"
        if type(field.read_back) is type(field.expected) and field.read_back == field.expected:
            return "match"
        return "mismatch"
    if field.read_back is None:
        return "unverified"
    if field.expected is not None and (
        type(field.entered) is not type(field.expected) or field.entered != field.expected
    ):
        return "mismatch"
    # Equality must include type: True and 1 must not count as the same value.
    if type(field.entered) is type(field.read_back) and field.entered == field.read_back:
        return "match"
    return "mismatch"


def append_field_captures(ledger_path: str, payload: dict) -> dict:
    """Atomically append one structured event per field and return counts only."""
    batch = FieldCaptureBatch.model_validate(payload)
    events = []
    counts: Counter[str] = Counter()
    for field in batch.fields:
        verification = _verification(field)
        counts[verification] += 1
        events.append(
            {
                "event_id": field.event_id,
                "job_url": batch.job_url,
                "company": batch.company,
                "title": batch.title,
                "event_type": "form_field",
                "timestamp": field.timestamp,
                "observed": {"label": field.label, "value": field.before},
                "entered": {"label": field.label, "value": field.entered},
                "artifact_paths": field.artifact_paths,
                "details": {
                    "control": field.control,
                    "step": field.step,
                    "required": field.required,
                    "action": field.action,
                    "source": field.source,
                    "read_back": {"label": field.label, "value": field.read_back},
                    "expected": {"label": field.label, "value": field.expected},
                    "verification": verification,
                    "historical_observation_not_verified_fact": True,
                },
            }
        )
    result = ActivityStore(ledger_path).append_batch({"run_id": batch.run_id, "events": events})
    return {
        "run_id": batch.run_id,
        "inserted": len(result["inserted"]),
        "duplicate_ids": len(result["duplicate_ids"]),
        "verification_counts": dict(counts),
        "requires_attention": bool(counts["mismatch"] or counts["unverified"]),
    }
