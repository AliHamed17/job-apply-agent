"""Import explicitly reviewed recording observations into the private activity log.

This module never reads the video content, makes a factual inference, updates a
job outcome, or initiates a browser action. A reviewer supplies the observations.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from batch_apply.activity import ActivityStore, reject_secrets


class RecordingObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    offset_seconds: float = Field(ge=0, allow_inf_nan=False)
    job_url: str = Field(min_length=1)
    company: str = ""
    title: str = ""
    step: Literal[
        "job_opened",
        "form_filled",
        "cv_selected",
        "cv_upload",
        "submit_clicked",
        "receipt_visible",
        "prior_applied",
        "blocker",
        "other",
    ]
    description: str = Field(min_length=1)

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("An observation needs a description")
        return value


class RecordingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed: Literal[True]
    recording_path: str = Field(min_length=1)
    recording_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    recorded_at: datetime
    observations: list[RecordingObservation] = Field(min_length=1)

    @field_validator("reviewed", mode="before")
    @classmethod
    def require_explicit_review(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Recording observations must be explicitly reviewed")
        return value

    @field_validator("recorded_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Recording start time must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def unique_observations(self) -> RecordingReview:
        ids = [observation.id for observation in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("Observation IDs must be unique within a review")
        return self


def import_review(payload: dict, ledger_path: str | Path) -> dict:
    """Atomically append reviewed observations, or reject without saving events."""
    reject_secrets(payload)
    review = RecordingReview.model_validate(payload)
    video = Path(review.recording_path).resolve(strict=True)
    if not video.is_file():
        raise ValueError("Recording must be a regular file")
    hasher = hashlib.sha256()
    with video.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual_digest = hasher.hexdigest()
    if actual_digest != review.recording_sha256.lower():
        raise ValueError("Recording hash does not match the review")

    run_id = f"recording:{actual_digest}"
    events = []
    for observation in review.observations:
        events.append(
            {
                "event_id": f"{run_id}:{observation.id}",
                "job_url": observation.job_url,
                "company": observation.company,
                "title": observation.title,
                "event_type": "observation",
                "observed": observation.description,
                "timestamp": (review.recorded_at + timedelta(seconds=observation.offset_seconds)),
                "artifact_paths": [str(video)],
                "details": {
                    "source_type": "reviewed_screen_recording",
                    "recording_sha256": actual_digest,
                    "offset_seconds": observation.offset_seconds,
                    "step": observation.step,
                },
            }
        )
    return ActivityStore(ledger_path).append_batch({"run_id": run_id, "events": events})
