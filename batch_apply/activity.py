"""Append-only, local audit trail for observed browser application steps."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from batch_apply.models import utc_now
from batch_apply.urls import canonical_job_url, job_key

_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "otp",
    "onetimecode",
    "onetimepassword",
    "verificationcode",
    "securitycode",
    "authcode",
    "authorizationcode",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "sessiontoken",
    "sessionid",
    "cookie",
    "cookies",
    "authorization",
    "apikey",
    "clientsecret",
    "secret",
    "recoveryanswer",
    "securityanswer",
    "backupcode",
    "recoverycode",
    "otpcode",
    "verificationtoken",
    "csrf",
    "csrftoken",
    "xsrf",
    "xsrftoken",
    "token",
}
_LABEL_KEYS = {"label", "field", "fieldname", "name", "question"}
_VALUE_KEYS = {"value", "answer", "entered", "observed", "text", "content"}
_INLINE_SECRET = re.compile(
    r"(?:password|passwd|otp|verification[ _-]?code|(?:access|refresh|session)[ _-]?token|"
    r"cookie|authorization)[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+",
    re.I,
)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _sensitive_label(value: str) -> bool:
    normalized = _key(value)
    return (
        normalized in _SECRET_KEYS
        or any(
            term in normalized
            for term in (
                "password",
                "verificationcode",
                "onetimecode",
                "sessiontoken",
                "securityanswer",
                "recoveryanswer",
            )
        )
        or bool(
            re.search(
                r"(?:verification|one.?time|security|authentication|2fa|mfa|two.factor).*"
                r"(?:code|token)|(?:enter|digit).*code|סיסמ|קוד.*(?:אימות|חד.?פעמי)",
                value,
                re.I,
            )
        )
    )


def reject_secrets(value: Any) -> None:
    """Reject secret-bearing structures before serialization; never echo a value.

    This catches common credential labels and token URLs, not arbitrary secrets
    hidden in prose. Callers must also sanitize source artifacts before saving.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if _key(str(key)) in _SECRET_KEYS and item not in (None, "", [], {}):
                raise ValueError("Activity input contains a prohibited credential or session value")
        labelled_secret = any(
            _key(str(key)) in _LABEL_KEYS and isinstance(item, str) and _sensitive_label(item)
            for key, item in value.items()
        )
        if labelled_secret and any(
            _key(str(key)) in _VALUE_KEYS and item not in (None, "", [], {})
            for key, item in value.items()
        ):
            raise ValueError("Activity input contains a prohibited credential field value")
        for item in value.values():
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str):
        if _INLINE_SECRET.search(value) or re.search(r"\bBearer\s+\S+", value, re.I):
            raise ValueError("Activity text contains a prohibited credential or session value")
        if value.strip().lower().startswith(("https://", "http://")):
            parsed = urlsplit(value.strip())
            if (
                parsed.username
                or parsed.password
                or any(
                    _key(key) in _SECRET_KEYS | {"code", "auth", "session"}
                    for key, _ in parse_qsl(
                        parsed.query + "&" + parsed.fragment, keep_blank_values=True
                    )
                )
            ):
                raise ValueError(
                    "Activity URL contains a prohibited credential or session parameter"
                )


class ActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    job_url: str
    company: str = ""
    title: str = ""
    event_type: Literal[
        "observation",
        "form_field",
        "upload",
        "navigation",
        "submit_attempt",
        "confirmation",
        "blocker",
    ]
    observed: Any = None
    entered: Any = None
    cv_path: str = ""
    cv_sha256: str = ""
    artifact_paths: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Activity timestamps must include a timezone")
        return value.astimezone(UTC)


class ActivityBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=200)
    events: list[ActivityEvent] = Field(min_length=1)


class ActivityStore:
    """Independent audit table in the existing browser batch database.

    Logging a confirmation event never mutates the application outcome ledger.
    The executor records the separately validated outcome after observation.
    """

    def __init__(self, ledger_path: str | Path):
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS browser_batch_activity ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, "
                "run_id TEXT NOT NULL, job_id TEXT NOT NULL, event_type TEXT NOT NULL, "
                "timestamp TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS browser_batch_activity_run "
                "ON browser_batch_activity(run_id, sequence);"
                "CREATE INDEX IF NOT EXISTS browser_batch_activity_job "
                "ON browser_batch_activity(job_id, sequence);"
                "CREATE TRIGGER IF NOT EXISTS browser_batch_activity_no_update "
                "BEFORE UPDATE ON browser_batch_activity BEGIN "
                "SELECT RAISE(ABORT, 'Activity audit rows are append-only'); END;"
                "CREATE TRIGGER IF NOT EXISTS browser_batch_activity_no_delete "
                "BEFORE DELETE ON browser_batch_activity BEGIN "
                "SELECT RAISE(ABORT, 'Activity audit rows are append-only'); END;"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def append_batch(self, payload: dict | ActivityBatch) -> dict:
        raw = payload.model_dump(mode="json") if isinstance(payload, ActivityBatch) else payload
        reject_secrets(raw)
        batch = ActivityBatch.model_validate(raw)
        records = []
        # Resolve and validate the complete batch before opening the write transaction.
        for event in batch.events:
            record = event.model_dump(mode="json")
            record["run_id"] = batch.run_id
            record["canonical_url"] = canonical_job_url(event.job_url)
            record["job_id"] = job_key(event.job_url)
            record["artifact_paths"] = [str(Path(path).resolve()) for path in event.artifact_paths]
            if event.cv_path:
                cv_path = Path(event.cv_path).resolve(strict=True)
                digest = hashlib.sha256(cv_path.read_bytes()).hexdigest()
                if event.cv_sha256 and event.cv_sha256.lower() != digest:
                    raise ValueError("Activity CV hash does not match the referenced file")
                record["cv_path"] = str(cv_path)
                record["cv_sha256"] = digest
                record["cv_size_bytes"] = cv_path.stat().st_size
            elif event.cv_sha256:
                raise ValueError("A CV hash needs its source CV path")
            records.append(record)
        inserted, duplicate_ids = [], []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                rendered = json.dumps(record, ensure_ascii=False, sort_keys=True)
                previous = connection.execute(
                    "SELECT payload FROM browser_batch_activity WHERE event_id=?",
                    (record["event_id"],),
                ).fetchone()
                if previous:
                    if previous["payload"] != rendered:
                        raise ValueError("An existing activity event id cannot be changed")
                    duplicate_ids.append(record["event_id"])
                    continue
                cursor = connection.execute(
                    "INSERT INTO browser_batch_activity "
                    "(event_id,run_id,job_id,event_type,timestamp,recorded_at,payload) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        record["event_id"],
                        batch.run_id,
                        record["job_id"],
                        record["event_type"],
                        record["timestamp"],
                        utc_now(),
                        rendered,
                    ),
                )
                inserted.append({"event_id": record["event_id"], "sequence": cursor.lastrowid})
        return {"run_id": batch.run_id, "inserted": inserted, "duplicate_ids": duplicate_ids}

    def export(self, *, run_id: str | None = None, job_url: str | None = None) -> dict:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if job_url:
            clauses.append("job_id=?")
            params.append(job_key(job_url))
        query = "SELECT sequence,recorded_at,payload FROM browser_batch_activity"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        with self._connect() as connection:
            events = [
                dict(
                    json.loads(row["payload"]),
                    sequence=row["sequence"],
                    recorded_at=row["recorded_at"],
                )
                for row in connection.execute(query, params)
            ]
        return {"generated_at": utc_now(), "count": len(events), "events": events}
