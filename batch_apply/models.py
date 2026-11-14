"""Typed, local-only records for an explicitly supplied browser job inventory."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class BatchStatus(StrEnum):
    DISCOVERED = "discovered"
    PREPARED = "prepared"
    IN_PROGRESS = "in_progress"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    SUBMITTED = "submitted"
    ALREADY_APPLIED = "already_applied"
    BLOCKED = "blocked"
    CLOSED = "closed"
    SKIPPED = "skipped"


class TabSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1)
    title: str = ""
    company: str = ""
    description: str = ""
    tab_id: str = ""


class CVChoice(BaseModel):
    file: str
    path: str
    sha256: str
    size_bytes: int
    role: str
    source: str
    reason: str
    confidence: float = Field(ge=0, le=1)


class ObservedEvidence(BaseModel):
    kind: str
    text: str = Field(min_length=1)
    url: str = ""
    reference: str = ""
    artifact: str = ""
    observed_at: str = Field(default_factory=utc_now)


class BatchJob(BaseModel):
    job_id: str
    canonical_url: str
    title: str
    company: str = ""
    description: str = ""
    source_urls: list[str] = Field(default_factory=list)
    tab_ids: list[str] = Field(default_factory=list)
    status: BatchStatus = BatchStatus.DISCOVERED
    selected_cv: CVChoice | None = None
    reason: str = ""
    latest_evidence: ObservedEvidence | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
