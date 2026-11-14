"""Turn recorded evidence into reusable, local application work packets.

The active LLM reviews these packets and operates the browser. Historical form
values and web content are never promoted into verified applicant facts here.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from batch_apply.activity import reject_secrets
from batch_apply.answer_cache import (
    AnswerCache,
    load_answer_cache,
    lookup_answer,
    validate_cache_sources,
)
from batch_apply.catalog import choose_cv_file, recommend_cv, verify_cv
from batch_apply.employer_policy import evaluate_employer
from batch_apply.models import BatchJob, TabSnapshot, utc_now
from batch_apply.policy import resolve_no_input
from batch_apply.urls import canonical_job_url
from submitters.form_brain import AnswerResult, FieldSpec

WORKFLOW = [
    "Read the current job posting and employer from the live page.",
    "Check the current employer exclusion policy and live ledger/source URL aliases.",
    "Read the actual existing CV; record a reasoned selection and verify its hash.",
    "Plan and claim through the coordinator; compare returned URL with the intended job.",
    "Inspect the current form and bind each question to an exact supported fact.",
    "Fill supported answers; leave optional unknowns empty and block required unknowns.",
    "Upload the unchanged selected CV; verify the displayed attached filename.",
    "Recheck employer policy against the observed employer and redirected form URL.",
    "Review required fields and declarations; submit once under current user authorization.",
    "Observe a positive receipt; save evidence and record the outcome immediately.",
    "Reconcile uncertain attempts before any retry; continue to the next eligible job.",
]

_CONTACT_LABELS = {
    "full name": ("personal.name", "ordinary_job_contact", "identity"),
    "your name": ("personal.name", "ordinary_job_contact", "identity"),
    "name": ("personal.name", "ordinary_job_contact", "identity"),
    "first name": ("personal.name", "ordinary_job_contact", "first"),
    "last name": ("personal.name", "ordinary_job_contact", "last"),
    "surname": ("personal.name", "ordinary_job_contact", "last"),
    "email": ("personal.email", "ordinary_job_contact", "identity"),
    "email address": ("personal.email", "ordinary_job_contact", "identity"),
    "phone": ("personal.phone", "ordinary_job_contact", "identity"),
    "phone number": ("personal.phone", "ordinary_job_contact", "identity"),
    "mobile phone": ("personal.phone", "ordinary_job_contact", "identity"),
    "linkedin": ("links.linkedin", "public_professional_link", "identity"),
    "linkedin profile": ("links.linkedin", "public_professional_link", "identity"),
    "github": ("links.github", "public_professional_link", "identity"),
    "portfolio": ("links.portfolio", "public_professional_link", "identity"),
    "city": ("location.configured_city", "ordinary_job_city", "identity"),
}


class ObservedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: str = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    # Only the active LLM may supply these after comparing actual question meaning.
    fact_key: str = ""
    scope: str = ""
    meaning_reviewed: bool = False


class ObservedApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: TabSnapshot
    fields: list[ObservedField] = Field(default_factory=list)
    cv_file: str = ""
    cv_reason: str = ""


def _read_ledger(path: str | Path) -> tuple[list[BatchJob], list[dict]]:
    database = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        jobs = [
            BatchJob.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT payload FROM browser_batch_jobs ORDER BY canonical_url"
            )
        ]
        events = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM browser_batch_activity ORDER BY sequence"
            )
        ]
    return jobs, events


def source_provider(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for domain, label in [
        ("linkedin.com", "LinkedIn"),
        ("comeet.com", "Comeet"),
        ("comeet.co", "Comeet"),
        ("greenhouse.io", "Greenhouse"),
        ("lever.co", "Lever"),
        ("sapsf.com", "SuccessFactors"),
        ("successfactors.com", "SuccessFactors"),
        ("hibob.com", "HiBob"),
        ("smartrecruiters.com", "SmartRecruiters"),
    ]:
        if host == domain or host.endswith("." + domain):
            return label
    return host or "unknown"


def build_knowledge(
    *,
    ledger_path: str | Path,
    cache_path: str | Path,
    cv_directory: str | Path,
    policy_path: str | Path,
) -> dict:
    """Aggregate step types and evidence pointers, never raw entered answers."""
    cache = load_answer_cache(cache_path)
    issues = validate_cache_sources(cache)
    if issues:
        raise ValueError("Verified sources changed; refresh the answer cache first")
    jobs, events = _read_ledger(ledger_path)
    by_job = {job.job_id: job for job in jobs}
    providers: dict[str, dict] = {}
    sequences: dict[str, list[str]] = defaultdict(list)
    for event in events:
        job = by_job.get(event.get("job_id"))
        if job is None:
            continue
        provider = source_provider(job.canonical_url)
        group = providers.setdefault(provider, {"event_types": Counter(), "examples": []})
        kind = event.get("event_type", "unknown")
        group["event_types"][kind] += 1
        sequence = sequences[job.job_id]
        if not sequence or sequence[-1] != kind:
            sequence.append(kind)
    for job in jobs:
        provider = source_provider(job.canonical_url)
        group = providers.setdefault(provider, {"event_types": Counter(), "examples": []})
        if job.status == "submitted" and len(group["examples"]) < 3:
            group["examples"].append(
                {
                    "job_id": job.job_id,
                    "job_url": job.canonical_url,
                    "step_types": sequences[job.job_id],
                    "receipt_artifact": job.latest_evidence.artifact if job.latest_evidence else "",
                }
            )
    result = {
        "schema_version": "application-workflow-knowledge-v1",
        "generated_at": utc_now(),
        "ledger_path": str(Path(ledger_path).resolve()),
        "answer_cache_path": str(Path(cache_path).resolve()),
        "protected_source_paths": [source.path for source in cache.source_files],
        "cv_directory": str(Path(cv_directory).resolve(strict=True)),
        "employer_policy_path": str(Path(policy_path).resolve()),
        "counts": dict(Counter(job.status.value for job in jobs)),
        "activity_event_count": len(events),
        "verified_fact_index": [
            {
                "key": fact.key,
                "scope": fact.scope,
                "basis": fact.basis,
                "source_path": fact.source_path,
            }
            for fact in cache.facts
        ],
        "historical_cv_routes": cache.cv_routes,
        "source_provider_patterns": providers,
        "execution_steps": WORKFLOW,
        "limits": [
            "Knowledge is a snapshot; packets reread the live ledger, policy and fact sources.",
            "Recorded form entries, employer requirements and successful receipts are not facts.",
            "Provider patterns group source URLs; redirected company forms need fresh inspection.",
            "Historical CV routes suggest choices; they do not establish fit or qualifications.",
            "No saved locator, coordinate, secret, cookie, raw form answer or background executor.",
            "Packets need an active LLM, supported browser tools and current authorization.",
        ],
    }
    reject_secrets(result)
    return result


def _field_decision(cache: AnswerCache, field: ObservedField) -> dict:
    spec = FieldSpec(field.label, field.kind, field.options, field.required)
    binding = _CONTACT_LABELS.get(field.label.strip().rstrip("*: ").casefold())
    if field.fact_key or field.scope:
        # User/site-provided metadata cannot silently remap a contact label.
        if binding:
            if not (field.meaning_reviewed and (field.fact_key, field.scope) == binding[:2]):
                binding = None
        elif (
            field.meaning_reviewed
            and field.fact_key.startswith("experience.")
            and field.kind in {"text", "textarea", "number", "select", "radio"}
        ):
            binding = (field.fact_key, field.scope, "identity")
        else:
            binding = None
    if field.kind not in {"text", "textarea", "number", "select", "radio", "email", "tel", "url"}:
        binding = None
    if binding and field.kind == "number" and not binding[0].startswith("experience."):
        binding = None
    fact = lookup_answer(cache, binding[0], scope=binding[1]) if binding else None
    supported = None
    if fact:
        value = str(fact.value)
        if binding[2] == "first":
            value = value.split()[0]
        elif binding[2] == "last":
            value = " ".join(value.split()[1:])
        supported = AnswerResult(value, "verified_scoped_evidence", True)
    decision = asdict(resolve_no_input(spec, supported))
    return {
        "field_id": field.field_id,
        "label": field.label,
        "kind": field.kind,
        "required": field.required,
        **decision,
        "fact_key": fact.key if fact else None,
        "source_path": fact.source_path if fact else None,
        "source_selector": fact.source_selector if fact else None,
    }


def prepare_packet(
    application: ObservedApplication,
    *,
    ledger_path: str | Path,
    cache_path: str | Path,
    cv_directory: str | Path,
    policy_path: str | Path,
) -> dict:
    """Read live eligibility and resolve facts; never claim a job or submit a form."""
    reject_secrets(application.model_dump(mode="json"))
    canonical = canonical_job_url(application.job.url)
    gate = evaluate_employer(
        application.job.company, [application.job.url], policy_path=policy_path
    )
    packet = {
        "schema_version": "application-work-packet-v1",
        "generated_at": utc_now(),
        "job": {
            "url": canonical,
            "title": application.job.title,
            "company": application.job.company,
        },
        "employer_check": gate,
        "next_action": "review_and_claim",
        "ledger_modified": False,
        "browser_actions_performed": False,
        "submission_ready": False,
        "requires_live_review": True,
        "execution_steps": WORKFLOW,
        "fields": [],
        "cv_candidate": None,
    }
    if not gate["allowed"]:
        packet["next_action"] = "skip_excluded_employer"
        return packet
    jobs, _ = _read_ledger(ledger_path)
    matches = [
        job
        for job in jobs
        if canonical in {canonical_job_url(url) for url in [job.canonical_url, *job.source_urls]}
    ]
    stored = matches[0] if matches else None
    for job in matches:
        stored_gate = evaluate_employer(
            job.company, [job.canonical_url, *job.source_urls], policy_path=policy_path
        )
        if not stored_gate["allowed"]:
            packet.update(employer_check=stored_gate, next_action="skip_excluded_employer")
            return packet
    if stored:
        packet["existing_job"] = {"job_id": stored.job_id, "status": stored.status.value}
        statuses = {job.status.value for job in matches}
        if statuses & {"submitted", "already_applied", "closed", "skipped"}:
            packet["next_action"] = "skip_existing_outcome"
            return packet
        if statuses & {"in_progress", "submission_uncertain"}:
            packet["next_action"] = "reconcile_existing_attempt"
            return packet
        if "blocked" in statuses:
            packet["next_action"] = "review_existing_blocker"
            return packet
    cache = load_answer_cache(cache_path)
    issues = validate_cache_sources(cache)
    if issues:
        packet.update(next_action="refresh_verified_sources", source_issues=issues)
        return packet
    ids = [field.field_id for field in application.fields]
    if len(ids) != len(set(ids)):
        raise ValueError("Observed field identifiers must be unique")
    packet["fields"] = [_field_decision(cache, field) for field in application.fields]
    if any(field["action"] == "block_job" for field in packet["fields"]):
        packet["next_action"] = "block_unsupported_required_fields"
    choice = stored.selected_cv if stored else None
    if application.cv_file:
        try:
            choice = choose_cv_file(
                cv_directory,
                application.cv_file,
                source="llm_browser_review",
                reason=application.cv_reason,
            )
        except FileNotFoundError:
            packet["next_action"] = "review_and_select_cv"
            return packet
    elif choice is None:
        choice, reason = recommend_cv(application.job, cv_directory)
        packet["cv_suggestion_reason"] = reason
    if choice is not None:
        if not verify_cv(choice):
            packet["next_action"] = "review_changed_cv"
        else:
            packet["cv_candidate"] = choice.model_dump(mode="json")
            for field, decision in zip(application.fields, packet["fields"], strict=True):
                if field.kind == "file" and field.label.strip().rstrip("*: ").casefold() in {
                    "resume",
                    "cv",
                    "resume/cv",
                    "upload resume",
                    "upload cv",
                    "curriculum vitae",
                }:
                    decision.update(
                        action="upload_cv",
                        value=choice.path,
                        source="verified_cv_identity",
                        reason="unchanged_cv_candidate",
                    )
            if packet["next_action"] == "block_unsupported_required_fields" and not any(
                f["action"] == "block_job" for f in packet["fields"]
            ):
                packet["next_action"] = "review_and_claim"
    if choice is None and packet["next_action"] == "review_and_claim":
        packet["next_action"] = "review_and_select_cv"
    # Return data only: a prepared packet cannot be used as permission to submit.
    reject_secrets(packet)
    return packet


def write_private_json(path: str | Path, payload: dict, *, inputs: list[str | Path]) -> None:
    """Avoid overwriting source files and atomically replace the requested output."""
    destination = Path(path).resolve()
    if destination.suffix.lower() != ".json" or destination in {
        Path(source).resolve() for source in inputs
    }:
        raise ValueError("Output must be a separate JSON file")
    reject_secrets(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4  # noqa: PLC0415

    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
