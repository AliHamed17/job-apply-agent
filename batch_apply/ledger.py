"""Transactional local ledger with explicit evidence and guarded recovery."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from batch_apply.activity import reject_secrets
from batch_apply.catalog import verify_cv
from batch_apply.employer_policy import EmployerPolicyError, evaluate_employer
from batch_apply.models import (
    BatchJob,
    BatchStatus,
    CVChoice,
    ObservedEvidence,
    TabSnapshot,
    utc_now,
)
from batch_apply.urls import canonical_job_url, job_key

_FINAL = {
    BatchStatus.SUBMITTED,
    BatchStatus.ALREADY_APPLIED,
    BatchStatus.CLOSED,
    BatchStatus.SKIPPED,
}

_CONFIRMATION = re.compile(
    r"application\s+(?:was\s+|has\s+been\s+)?(?:submitted|received|sent)|"
    r"(?:successfully|success)\s+(?:submitted|applied)|"
    r"(?:thank\s+you|thanks)\s+for\s+(?:applying|your\s+application\b)|"
    r"\bwe(?:['’]ve|\s+have)\s+received\s+your\s+application\b(?!\s+draft\b)|"
    r"הבקשה.{0,25}(?:נשלחה|התקבלה)|מועמדות.{0,25}(?:נשלחה|התקבלה)",
    re.IGNORECASE,
)

_NEGATIVE_CONFIRMATION = re.compile(
    r"\b(?:application|submission)\s+(?:(?:was|is|could|can|has|have)\s+)?"
    r"(?:not|never)\s+(?:(?:be|been|yet|successfully)\s+){0,2}"
    r"(?:submitted|received|sent)\b|"
    r"\b(?:not|never)\s+(?:(?:yet|successfully)\s+)?(?:received|submitted|sent)\s+"
    r"(?:(?:your|the|an?)\s+)?application\b|"
    r"\b(?:unable|failed)\s+to\s+(?:submit|send)\s+"
    r"(?:(?:your|the|an?)\s+)?application\b|"
    r"\b(?:unable|failed)\s+to\s+apply\b|"
    r"\bno\s+application\s+(?:(?:was|has\s+been)\s+)?(?:submitted|received|sent)\b|"
    r"\b(?:application|submission)\s+(?:has\s+)?(?:failed|unsuccessful)\b|"
    r"\b(?:application|submission)\s+(?:(?:was|is|has\s+been)\s+)?"
    r"(?:submitted|sent|received)\s+unsuccessfully\b|"
    r"^\s*(?:not|never)\s+(?:successfully\s+)?(?:submitted|received|sent|applied)\b|"
    r"(?:הבקשה|בקשתך|מועמדותך|המועמדות)\s+לא\s+(?:נשלח(?:ה)?|התקבל(?:ה)?)",
    re.IGNORECASE,
)


def cv_selection_allowed(job: BatchJob) -> bool:
    """A new CV only resolves CV blockers, never an observed site/form blocker."""
    if job.status in {BatchStatus.DISCOVERED, BatchStatus.PREPARED}:
        return True
    return job.status == BatchStatus.BLOCKED and job.reason.partition(":")[0].strip() in {
        "cv_selection_needed",
        "selected_cv_missing_or_changed",
    }


class BatchLedger:
    def __init__(self, path: str | Path, *, employer_policy_path: str | Path | None = None):
        self.path = Path(path)
        self.employer_policy_path = (
            Path(employer_policy_path)
            if employer_policy_path is not None
            else self.path.parent / "employer-policy.json"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS browser_batch_jobs ("
                "job_id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS browser_batch_events ("
                "event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, "
                "created_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);"
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

    def check_employer(
        self,
        url_or_key: str,
        *,
        observed_employer: str = "",
        observed_urls=(),
    ) -> dict:
        """Check stored identity and freshly observed employer/URLs without changing history."""
        observed_urls = [observed_urls] if isinstance(observed_urls, str) else list(observed_urls)
        job = self.get(url_or_key)
        urls = [job.canonical_url, *job.source_urls, *observed_urls]
        decision = evaluate_employer(job.company, urls, policy_path=self.employer_policy_path)
        if decision["allowed"] and observed_employer:
            decision = evaluate_employer(
                observed_employer, urls, policy_path=self.employer_policy_path
            )
        return {**decision, "job_id": job.job_id}

    def pre_submit_check(
        self,
        url_or_key: str,
        *,
        observed_employer: str = "",
        observed_urls=(),
    ) -> dict:
        """Fail closed before a browser Submit action, including redirects and policy edits."""
        observed_urls = [observed_urls] if isinstance(observed_urls, str) else list(observed_urls)
        if not observed_employer.strip() and not observed_urls:
            raise EmployerPolicyError("Pre-submit check needs the observed employer or page URL")
        for url in observed_urls:
            canonical_job_url(url)  # Reject local paths and malformed fresh page identities.
        decision = self.check_employer(
            url_or_key, observed_employer=observed_employer, observed_urls=observed_urls
        )
        if not decision["allowed"]:
            raise EmployerPolicyError(decision["reason"])
        job = self.get(url_or_key)
        if job.status != BatchStatus.IN_PROGRESS:
            raise ValueError("Pre-submit check requires an in-progress job")
        if not job.selected_cv or not verify_cv(job.selected_cv):
            raise ValueError("Selected CV is missing or changed")
        return decision

    def _exclude(self, connection: sqlite3.Connection, job: BatchJob) -> bool:
        decision = evaluate_employer(
            job.company,
            [job.canonical_url, *job.source_urls],
            policy_path=self.employer_policy_path,
        )
        if decision["allowed"]:
            return False
        job.status = BatchStatus.BLOCKED
        job.reason = decision["reason"]
        self._save(connection, job, "employer_policy_blocked", details=decision)
        return True

    @staticmethod
    def _load(connection: sqlite3.Connection, key: str) -> BatchJob:
        row = connection.execute(
            "SELECT payload FROM browser_batch_jobs WHERE job_id = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown batch job: {key}")
        payload = json.loads(row["payload"])
        reject_secrets(payload)
        return BatchJob.model_validate(payload)

    @staticmethod
    def _save(
        connection: sqlite3.Connection, job: BatchJob, event: str, *, details: dict | None = None
    ) -> None:
        reject_secrets(job.model_dump(mode="json"))
        reject_secrets(details)
        job.updated_at = utc_now()
        event_payload = {
            "event": event,
            "reason": job.reason,
            "evidence": job.latest_evidence.model_dump() if job.latest_evidence else None,
        }
        if details is not None:
            event_payload["details"] = details
        connection.execute(
            "INSERT INTO browser_batch_jobs(job_id, canonical_url, status, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "status=excluded.status, payload=excluded.payload",
            (job.job_id, job.canonical_url, job.status, job.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO browser_batch_events(job_id, created_at, status, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                job.job_id,
                job.updated_at,
                job.status,
                json.dumps(event_payload, ensure_ascii=False),
            ),
        )

    def import_tabs(self, tabs: list[TabSnapshot]) -> dict[str, int]:
        # Validate the complete inventory before writing any of it.
        reject_secrets([tab.model_dump(mode="json") for tab in tabs])
        pairs = [(tab, canonical_job_url(tab.url)) for tab in tabs]
        inserted = 0
        duplicates = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for tab, canonical in pairs:
                key = job_key(canonical)
                try:
                    job = self._load(connection, key)
                    duplicates += 1
                    if not job.title:
                        job.title = tab.title
                    if not job.company:
                        job.company = tab.company
                    if not job.description:
                        job.description = tab.description
                except KeyError:
                    job = BatchJob(
                        job_id=key,
                        canonical_url=canonical,
                        title=tab.title,
                        company=tab.company,
                        description=tab.description,
                    )
                    inserted += 1
                if tab.url not in job.source_urls:
                    job.source_urls.append(tab.url)
                if tab.tab_id and tab.tab_id not in job.tab_ids:
                    job.tab_ids.append(tab.tab_id)
                if job.status in {BatchStatus.DISCOVERED, BatchStatus.PREPARED}:
                    if self._exclude(connection, job):
                        continue
                self._save(connection, job, "tab_imported")
        return {"inserted": inserted, "duplicates": duplicates, "tabs": len(tabs)}

    def get(self, url_or_key: str) -> BatchJob:
        key = job_key(url_or_key) if "://" in url_or_key else url_or_key
        with self._connect() as connection:
            return self._load(connection, key)

    def list_jobs(self) -> list[BatchJob]:
        with self._connect() as connection:
            jobs = []
            for row in connection.execute("SELECT payload FROM browser_batch_jobs ORDER BY rowid"):
                payload = json.loads(row["payload"])
                reject_secrets(payload)
                jobs.append(BatchJob.model_validate(payload))
            return jobs

    def prepare(self, url_or_key: str, choice: CVChoice | None, reason: str) -> BatchJob:
        key = job_key(url_or_key) if "://" in url_or_key else url_or_key
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._load(connection, key)
            # Reimporting/replanning must never requeue a submitted or uncertain job.
            if job.status not in {BatchStatus.DISCOVERED, BatchStatus.PREPARED}:
                return job
            if self._exclude(connection, job):
                return job
            if choice is not None and not verify_cv(choice):
                raise ValueError("Selected CV is missing or changed")
            job.selected_cv = choice
            job.status = BatchStatus.PREPARED if choice else BatchStatus.BLOCKED
            job.reason = reason
            self._save(connection, job, "planned")
            return job

    def select_cv(self, url_or_key: str, choice: CVChoice) -> BatchJob:
        """An explicit LLM decision may resolve a blocked CV match, with provenance."""
        key = job_key(url_or_key) if "://" in url_or_key else url_or_key
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._load(connection, key)
            if job.status in _FINAL or job.status in {
                BatchStatus.IN_PROGRESS,
                BatchStatus.SUBMISSION_UNCERTAIN,
            }:
                raise ValueError("Cannot replace a CV after work started or a terminal outcome")
            if self._exclude(connection, job):
                return job
            if not cv_selection_allowed(job):
                raise ValueError("A CV selection cannot resolve this non-CV blocker")
            if not verify_cv(choice):
                raise ValueError("Selected CV is missing or changed")
            job.selected_cv = choice
            job.status = BatchStatus.PREPARED
            job.reason = choice.reason
            self._save(connection, job, "cv_selected")
            return job

    def claim_next(self) -> BatchJob | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT job_id FROM browser_batch_jobs WHERE status=? ORDER BY rowid",
                (BatchStatus.PREPARED,),
            ).fetchall()
            job = None
            for row in rows:
                candidate = self._load(connection, row["job_id"])
                if not self._exclude(connection, candidate):
                    job = candidate
                    break
            if job is None:
                return None
            if not job.selected_cv or not verify_cv(job.selected_cv):
                job.status = BatchStatus.BLOCKED
                job.reason = "selected_cv_missing_or_changed"
                self._save(connection, job, "cv_verification_failed")
                return job
            job.status = BatchStatus.IN_PROGRESS
            job.reason = "claimed_for_browser_execution"
            self._save(connection, job, "claimed")
            return job

    def resume_blocked(self, url_or_key: str, choice: CVChoice, resolution: str) -> BatchJob:
        """Explicitly resume one blocked job after current authorization or new facts."""
        resolution = resolution.strip()
        if not resolution:
            raise ValueError("A nonempty blocker resolution note is required")
        key = job_key(url_or_key) if "://" in url_or_key else url_or_key
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._load(connection, key)
            if job.status != BatchStatus.BLOCKED:
                raise ValueError("Only a blocked job can be explicitly resumed")
            decision = self.check_employer(url_or_key)
            if not decision["allowed"]:
                raise EmployerPolicyError(decision["reason"])
            if not verify_cv(choice):
                raise ValueError("Selected CV is missing or changed")
            job.selected_cv = choice
            job.status = BatchStatus.PREPARED
            job.reason = resolution
            job.latest_evidence = ObservedEvidence(
                kind="blocker_resume_note", text=resolution, url=job.canonical_url
            )
            self._save(connection, job, "blocker_resumed")
            return job

    def reconsider_skipped(
        self, url_or_key: str, choice: CVChoice, resolution: str, evidence: ObservedEvidence
    ) -> BatchJob:
        """Revisit a never-submitted skip only after explicit new facts or observed state."""
        resolution = resolution.strip()
        if not resolution:
            raise ValueError("A nonempty skip resolution note is required")
        if evidence.kind not in {"new_factual_evidence", "new_observed_state"}:
            raise ValueError("Skip reconsideration requires explicit new factual or state evidence")
        if not evidence.text.strip() or not evidence.artifact.strip():
            raise ValueError("New evidence needs a nonempty description and artifact")
        artifact = Path(evidence.artifact).resolve()
        if not artifact.is_file() or not artifact.stat().st_size:
            raise ValueError("New evidence artifact must be an existing nonempty file")
        artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
        observed_at = datetime.fromisoformat(evidence.observed_at)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("New evidence observation must include a timezone")
        key = job_key(url_or_key) if "://" in url_or_key else url_or_key
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._load(connection, key)
            if job.status != BatchStatus.SKIPPED:
                raise ValueError("Only a skipped job can be explicitly reconsidered")
            decision = self.check_employer(url_or_key)
            if not decision["allowed"]:
                raise EmployerPolicyError(decision["reason"])
            history_keys = sorted({key, *(job_key(url) for url in job.source_urls)})
            placeholders = ",".join("?" for _ in history_keys)
            for alias in connection.execute(
                f"SELECT job_id, status FROM browser_batch_jobs WHERE job_id IN ({placeholders})",
                history_keys,
            ):
                if alias["job_id"] != key and alias["status"] != BatchStatus.SKIPPED:
                    raise ValueError("Cannot reconsider an active or terminal source URL alias")
            if not job.selected_cv or not verify_cv(job.selected_cv) or not verify_cv(choice):
                raise ValueError("The original selected CV is missing or changed")
            if (
                any(
                    getattr(choice, field) != getattr(job.selected_cv, field)
                    for field in ("file", "sha256", "size_bytes")
                )
                or Path(choice.path).resolve() != Path(job.selected_cv.path).resolve()
            ):
                raise ValueError("Skip reconsideration must retain the original selected CV")
            skipped_at = []
            for row in connection.execute(
                f"SELECT created_at, status, payload FROM browser_batch_events "
                f"WHERE job_id IN ({placeholders}) ORDER BY event_id",
                history_keys,
            ):
                prior = json.loads(row["payload"])
                prior_evidence = prior.get("evidence") or {}
                if (
                    row["status"]
                    in {
                        BatchStatus.SUBMITTED,
                        BatchStatus.ALREADY_APPLIED,
                        BatchStatus.SUBMISSION_UNCERTAIN,
                        BatchStatus.CLOSED,
                    }
                    or prior_evidence.get("kind")
                    in {
                        "submit_clicked",
                        "submit_attempt",
                        "page_confirmation",
                        "email_confirmation",
                        "already_applied_notice",
                    }
                    or prior.get("event") == "submit_attempt"
                ):
                    raise ValueError("Cannot reconsider a prior submission or closed history")
                if row["status"] == BatchStatus.SKIPPED:
                    skipped_at.append(datetime.fromisoformat(row["created_at"]))
                if prior_evidence.get("artifact"):
                    old_artifact = Path(prior_evidence["artifact"]).resolve()
                    if old_artifact == artifact or (
                        old_artifact.is_file()
                        and hashlib.sha256(old_artifact.read_bytes()).hexdigest() == artifact_sha256
                    ):
                        raise ValueError("Reconsideration needs a new evidence artifact")
            if any(t.tzinfo is None or t.utcoffset() is None for t in skipped_at):
                raise ValueError("Recorded skip timestamps must include a timezone")
            if not skipped_at or observed_at <= max(skipped_at):
                raise ValueError("New evidence must be observed after the recorded skip")
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_batch_activity'"
            ).fetchone():
                raise ValueError("Browser activity history is required before reconsideration")
            for row in connection.execute(
                f"SELECT event_type, timestamp, payload FROM browser_batch_activity "
                f"WHERE job_id IN ({placeholders})",
                history_keys,
            ):
                activity = json.loads(row["payload"])
                if row["event_type"] in {"submit_attempt", "confirmation"} or activity.get(
                    "event_type"
                ) in {"submit_attempt", "confirmation"}:
                    raise ValueError("Cannot reconsider a prior submission activity")
                if datetime.fromisoformat(row["timestamp"]) <= max(skipped_at):
                    for previous_path in activity.get("artifact_paths", []):
                        old_artifact = Path(previous_path).resolve()
                        if old_artifact == artifact or (
                            old_artifact.is_file()
                            and hashlib.sha256(old_artifact.read_bytes()).hexdigest()
                            == artifact_sha256
                        ):
                            raise ValueError("Reconsideration needs a new evidence artifact")
            prior_choice = job.selected_cv.model_dump()
            job.selected_cv = choice
            job.status = BatchStatus.PREPARED
            job.reason = resolution
            job.latest_evidence = evidence
            self._save(
                connection,
                job,
                "skipped_reconsidered",
                details={
                    "previous_selected_cv": prior_choice,
                    "evidence_artifact_sha256": artifact_sha256,
                    "verified_cv_sha256": choice.sha256,
                },
            )
            return job

    def record(self, url_or_key: str, status: BatchStatus, evidence: ObservedEvidence) -> BatchJob:
        reject_secrets(evidence.model_dump(mode="json"))
        status = BatchStatus(status)
        if status in {BatchStatus.DISCOVERED, BatchStatus.PREPARED, BatchStatus.IN_PROGRESS}:
            raise ValueError("Use import, CV selection, or claim for work states")
        if status == BatchStatus.SUBMITTED:
            if evidence.kind not in {"page_confirmation", "email_confirmation"}:
                raise ValueError("Submitted requires an observed page/email confirmation")
        if status == BatchStatus.ALREADY_APPLIED and evidence.kind != "already_applied_notice":
            raise ValueError("Already-applied needs an observed prior application notice")
        if status == BatchStatus.SUBMISSION_UNCERTAIN and evidence.kind != "submit_clicked":
            raise ValueError("Uncertain submission needs evidence of the submit attempt")
        key = job_key(url_or_key) if "://" in url_or_key else url_or_key
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._load(connection, key)
            if status == BatchStatus.SUBMITTED and _NEGATIVE_CONFIRMATION.search(evidence.text):
                raise ValueError(
                    "Confirmation evidence must identify a positive application result"
                )
            if (
                status == BatchStatus.SUBMITTED
                and (not evidence.reference)
                and (not _CONFIRMATION.search(evidence.text))
            ):
                raise ValueError(
                    "Confirmation evidence must identify a received/submitted application"
                )
            if job.status in _FINAL:
                if job.status == status:
                    return job
                raise ValueError("A terminal outcome cannot be reset or submitted again")
            if job.status == BatchStatus.SUBMISSION_UNCERTAIN and status not in {
                BatchStatus.SUBMITTED,
                BatchStatus.ALREADY_APPLIED,
            }:
                if (
                    status == BatchStatus.SUBMISSION_UNCERTAIN
                    and job.latest_evidence is not None
                    and all(
                        getattr(job.latest_evidence, field) == getattr(evidence, field)
                        for field in ("kind", "text", "url", "reference", "artifact")
                    )
                ):
                    return job
                raise ValueError("Reconcile an uncertain attempt before any retry or other outcome")
            job.status = status
            job.reason = evidence.text
            job.latest_evidence = evidence
            self._save(connection, job, "observed_outcome")
            return job

    def export(self) -> dict:
        jobs = self.list_jobs()
        counts = {state.value: sum(job.status == state for job in jobs) for state in BatchStatus}
        return {
            "generated_at": utc_now(),
            "executor": "external_llm_browser_runtime",
            "planning_is_submission": False,
            "counts": counts,
            "jobs": [job.model_dump(mode="json") for job in jobs],
        }
