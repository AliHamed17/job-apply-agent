"""Local evidence cache for an active LLM executor; never fills or submits forms."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from batch_apply.activity import reject_secrets
from batch_apply.catalog import verify_cv
from batch_apply.models import BatchJob, CVChoice, utc_now
from batch_apply.urls import canonical_job_url

_CONTACT = {
    "personal.name": "ordinary_job_contact",
    "personal.email": "ordinary_job_contact",
    "personal.phone": "ordinary_job_contact",
    "links.linkedin": "public_professional_link",
    "links.github": "public_professional_link",
    "links.portfolio": "public_professional_link",
}
_UNKNOWN = {
    "experience.python_years": r"python",
    "experience.pytorch_years": r"pytorch",
    "experience.ssis_years": r"ssis",
    "experience.ml_years": r"machine learning|\bml\b|deep learning",
    "experience.genai_years": r"genai|generative ai|lora",
    "salary.expectation": r"salary|שכר",
    "availability.notice_or_start": r"notice|start date|availability|commute",
    "travel.commitment": r"travel|overseas",
}
_CV_SOURCES = {
    "llm_browser_review",
    "live_llm_browser_review",
    "live_llm_job_review",
    "recording_selection",
    "explicit_blocker_resume",
    "explicit_skip_reconsideration",
}
_PROVENANCE = {
    key: (
        "personal"
        if key.startswith("personal.")
        else "portfolio"
        if key == "links.portfolio"
        else "linkedin_github"
    )
    for key in _CONTACT
}


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str
    size_bytes: int


class CachedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: StrictStr | StrictInt | StrictFloat
    scope: str
    basis: Literal["observed_profile_overlay", "configured_city", "reviewed_evidence"]
    source_path: str
    source_selector: str
    limits: str

    @model_validator(mode="after")
    def whitelisted_scope(self):
        expected = _CONTACT.get(self.key)
        if expected:
            if (
                self.scope != expected
                or self.basis != "observed_profile_overlay"
                or self.source_selector != self.key
                or _ordinary_value(self.key, self.value) != self.value
            ):
                raise ValueError("Invalid cached contact source or scope")
        elif self.key == "location.configured_city":
            if (
                self.scope != "ordinary_job_city"
                or self.basis != "configured_city"
                or self.source_selector != "personal.location"
                or not isinstance(self.value, str)
                or not self.value.strip()
                or any(character.isdigit() for character in self.value)
            ):
                raise ValueError("Invalid configured city scope")
        elif re.fullmatch(r"experience\.[a-z][a-z0-9_]{0,60}_years", self.key):
            if (
                self.basis != "reviewed_evidence"
                or not self.source_selector.startswith("facts.")
                or type(self.value) not in {int, float}
                or not 0 <= self.value <= 80
                or not self.scope.strip()
                or len(self.scope) > 250
            ):
                raise ValueError("Experience needs an exact reviewed numeric answer and scope")
        else:
            raise ValueError("Unsupported cached answer key")
        return self


class AnswerCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["verified-answer-cv-cache-v1"] = "verified-answer-cv-cache-v1"
    generated_at: str = Field(default_factory=utc_now)
    facts: list[CachedAnswer] = Field(default_factory=list)
    unknowns: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    cv_routes: list[dict] = Field(default_factory=list)
    source_files: list[SourceFile] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=lambda: [
            "Cache is evidence, not permission or a browser executor.",
            "An application/prefill/selected CV does not verify a factual answer.",
            "CV routes are historical suggestions; review the actual job and unchanged CV.",
            "Use the live ledger/claim coordinator for eligibility and duplicate prevention.",
            "CAPTCHA, Terms and credential rules still apply at action time.",
        ]
    )
    _sources_verified: bool = PrivateAttr(default=False)


def _fingerprint(path: str | Path) -> SourceFile:
    source = Path(path).resolve(strict=True)
    data = source.read_bytes()
    return SourceFile(
        path=str(source), sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    )


def validate_cache_sources(cache: AnswerCache) -> list[str]:
    """Verify once per loaded session; a stale source disables every cached answer."""
    issues = []
    for source in cache.source_files:
        try:
            if _fingerprint(source.path) != source:
                issues.append("cache_source_changed")
        except OSError:
            issues.append("cache_source_missing")
    if issues:
        cache._sources_verified = False
        return sorted(set(issues))
    fingerprints = {source.path: source for source in cache.source_files}
    for fact in cache.facts:
        if fact.source_path not in fingerprints:
            issues.append("answer_source_not_bound")
            continue
        try:
            if fact.basis == "configured_city":
                source = yaml.safe_load(Path(fact.source_path).read_text(encoding="utf-8-sig"))
                value = (source.get("personal") or {}).get("location")
            else:
                source = json.loads(Path(fact.source_path).read_text(encoding="utf-8-sig"))
                if fact.basis == "observed_profile_overlay":
                    section, field = fact.key.split(".")
                    reviewed = (source.get("provenance") or {}).get(_PROVENANCE[fact.key])
                    value = (
                        _ordinary_value(fact.key, (source.get(section) or {}).get(field))
                        if reviewed
                        else None
                    )
                else:
                    entry_id = fact.source_selector.removeprefix("facts.")
                    entry = next(
                        item for item in _reviewed_entries(source) if item["id"] == entry_id
                    )
                    if entry["key"] != fact.key or entry["scope"] != fact.scope:
                        issues.append("answer_scope_changed")
                    required = [(proof["path"], proof["sha256"]) for proof in entry["proofs"]]
                    value = entry["value"]
                    for path, digest in required:
                        proof = fingerprints.get(str(Path(path).resolve()))
                        if proof is None or proof.sha256 != digest:
                            issues.append("reviewed_proof_not_bound")
            if isinstance(value, str):
                value = value.strip()
            if value != fact.value:
                issues.append("cached_answer_changed")
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            StopIteration,
            yaml.YAMLError,
        ):
            issues.append("answer_provenance_invalid")
    if len({fact.key for fact in cache.facts}) != len(cache.facts):
        issues.append("duplicate_answer_key")
    for route in cache.cv_routes:
        source = fingerprints.get(str(Path(route.get("path", "")).resolve()))
        if (
            source is None
            or source.sha256 != route.get("sha256")
            or Path(source.path).name != route.get("file")
            or Path(source.path).suffix.lower() != ".pdf"
        ):
            issues.append("cv_route_not_bound")
    cache._sources_verified = not issues
    return sorted(set(issues))


def lookup_answer(cache: AnswerCache, key: str, *, scope: str) -> CachedAnswer | None:
    """Exact fact/scope lookup; no fuzzy questions, tenure inference or fallback values."""
    if not cache._sources_verified:
        return None
    return next((fact for fact in cache.facts if fact.key == key and fact.scope == scope), None)


def load_answer_cache(path: str | Path) -> AnswerCache:
    cache = AnswerCache.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
    reject_secrets(cache.model_dump(mode="json"))
    validate_cache_sources(cache)
    return cache


def _ordinary_value(key: str, value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 250:
        return None
    value = value.strip()
    if re.search(r"jane doe|john doe|example\.(?:com|org|net)", value, re.I):
        return None
    if key == "personal.name" and not re.search(r"[^\W\d_]", value):
        return None
    if key == "personal.email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return None
    if key == "personal.phone" and not re.fullmatch(r"\+\d{8,15}", value):
        return None
    if key.startswith("links."):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return None
    reject_secrets(value)
    return value


def _one_page(choice: CVChoice) -> bool:
    try:
        from pypdf import PdfReader  # noqa: PLC0415
        from pypdf.errors import PdfReadError  # noqa: PLC0415
    except ImportError:
        return False
    try:
        reader = PdfReader(choice.path)
        return not reader.is_encrypted and len(reader.pages) == 1
    except (OSError, ValueError, PdfReadError):
        return False


def _upload_binds_cv(event: dict, job: BatchJob, choice: CVChoice) -> bool:
    """Normalize historical upload hashes only; conflicting identities stay unbound."""
    if event.get("event_type") != "upload" or event.get("job_id") != job.job_id:
        return False
    if event.get("job_url"):
        try:
            if canonical_job_url(event["job_url"]) != job.canonical_url:
                return False
        except (TypeError, ValueError):
            return False
    details = event.get("details") or {}
    if not isinstance(details, dict):
        return False
    digests = [
        value
        for value in (
            event.get("cv_sha256"),
            details.get("cv_sha256"),
            details.get("sha256"),
        )
        if value not in (None, "")
    ]
    return bool(digests) and all(
        isinstance(value, str)
        and re.fullmatch(r"[a-fA-F0-9]{64}", value)
        and value.casefold() == choice.sha256
        for value in digests
    )


def _reviewed_entries(manifest: dict) -> list[dict]:
    if not isinstance(manifest, dict):
        raise ValueError("Reviewed facts manifest must be an object")
    entries = manifest.get("facts")
    if not isinstance(entries, list):
        raise ValueError("Reviewed facts manifest needs a facts list")
    ids = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("reviewed") is not True
            or entry.get("inherited_prefill") is not False
            or entry.get("source_type") not in {"user_confirmation", "recording_active_answer"}
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", str(entry.get("id", "")))
        ):
            raise ValueError("Only explicit reviewed source facts can be cached")
        if entry["id"] in ids:
            raise ValueError("Reviewed fact ids must be unique")
        ids.add(entry["id"])
        proofs = entry.get("proofs")
        if (
            not isinstance(proofs, list)
            or not proofs
            or any(
                not isinstance(proof, dict)
                or not isinstance(proof.get("path"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", str(proof.get("sha256", "")))
                for proof in proofs
            )
        ):
            raise ValueError("Reviewed facts need exact evidence SHA256 fingerprints")
        if entry["source_type"] == "recording_active_answer" and (
            entry.get("active_entry_observed") is not True
            or len({proof["path"] for proof in proofs}) < 2
        ):
            raise ValueError("A recording answer needs active-entry review and multiple proofs")
    return entries


def build_answer_cache(
    *,
    ledger_path: str | Path,
    overlay_path: str | Path,
    profile_path: str | Path,
    cv_directory: str | Path,
    verified_facts_path: str | Path | None = None,
    examples_per_cv: int = 5,
) -> AnswerCache:
    """Read only SQLite and whitelisted source fields; never extract PDF text or credentials."""
    if not 1 <= examples_per_cv <= 20:
        raise ValueError("examples_per_cv must be between 1 and 20")
    cache = AnswerCache()
    sources: dict[str, SourceFile] = {}

    def remember(path: str | Path, expected_hash: str | None = None) -> SourceFile:
        source = _fingerprint(path)
        if expected_hash and source.sha256 != expected_hash:
            raise ValueError("Reviewed evidence file changed")
        sources[source.path] = source
        return source

    overlay_source = remember(overlay_path)
    overlay = json.loads(Path(overlay_source.path).read_text(encoding="utf-8-sig"))
    if not isinstance(overlay, dict):
        raise ValueError("Verified profile overlay must be an object")
    provenance = overlay.get("provenance") or {}
    for key, scope in _CONTACT.items():
        section, field = key.split(".")
        # No fallback to stale base-profile links or unreviewed observed field values.
        if not provenance.get(_PROVENANCE[key]):
            continue
        value = _ordinary_value(key, (overlay.get(section) or {}).get(field))
        if value is not None:
            cache.facts.append(
                CachedAnswer(
                    key=key,
                    value=value,
                    scope=scope,
                    basis="observed_profile_overlay",
                    source_path=overlay_source.path,
                    source_selector=key,
                    limits=(
                        "Ordinary job-form contact only; preserve the original CV and its email."
                    ),
                )
            )
    profile_source = remember(profile_path)
    # Deliberately bypass load_profile(): it can extract multi-page academic attachments.
    profile = yaml.safe_load(Path(profile_source.path).read_text(encoding="utf-8-sig")) or {}
    if not isinstance(profile, dict):
        raise ValueError("Profile must be an object")
    city = (profile.get("personal") or {}).get("location")
    if (
        isinstance(city, str)
        and city.strip()
        and len(city) < 150
        and not any(character.isdigit() for character in city)
    ):
        cache.facts.append(
            CachedAnswer(
                key="location.configured_city",
                value=city.strip(),
                scope="ordinary_job_city",
                basis="configured_city",
                source_path=profile_source.path,
                source_selector="personal.location",
                limits=(
                    "Configured locality only; no street, commute, travel or relocation inference."
                ),
            )
        )
    if verified_facts_path:
        manifest_source = remember(verified_facts_path)
        manifest = json.loads(Path(manifest_source.path).read_text(encoding="utf-8-sig"))
        for entry in _reviewed_entries(manifest):
            for proof in entry["proofs"]:
                remember(proof["path"], proof["sha256"])
            cache.facts.append(
                CachedAnswer(
                    key=entry["key"],
                    value=entry["value"],
                    scope=entry["scope"],
                    basis="reviewed_evidence",
                    source_path=manifest_source.path,
                    source_selector="facts." + entry["id"],
                    limits="Exact reviewed scope only; no other technology or tenure inference.",
                )
            )

    database = Path(ledger_path).resolve(strict=True)
    # Closing matters on Windows; SQLite context-manager exit alone does not close the handle.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        jobs = [
            BatchJob.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT payload FROM browser_batch_jobs ORDER BY canonical_url"
            )
        ]
        activity = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM browser_batch_activity "
                "WHERE event_type IN ('blocker','upload') ORDER BY sequence"
            )
        ]
    by_job = {job.job_id: job for job in jobs}
    for event in activity:
        job = by_job.get(event.get("job_id"))
        if job is None:
            # Activity payloads use the canonical source URL rather than the SQL job_id.
            job = next((item for item in jobs if item.canonical_url == event.get("job_url")), None)
        if not job or job.status in {"submitted", "already_applied"}:
            continue
        if event.get("event_type") != "blocker":
            continue
        text = str((event.get("details") or {}).get("reason", "")) + " " + job.reason
        for key, pattern in _UNKNOWN.items():
            if re.search(pattern, text, re.I):
                proof = {
                    "job_id": job.job_id,
                    "event_id": event["event_id"],
                    "run_id": event["run_id"],
                    "reason": "No reusable verified answer",
                }
                if proof not in cache.unknowns.setdefault(key, []):
                    cache.unknowns[key].append(proof)

    root = Path(cv_directory).resolve(strict=True)
    groups: dict[tuple[str, str], dict] = {}
    checked: dict[tuple[str, str, int], bool] = {}
    for job in jobs:
        choice = job.selected_cv
        if choice is None or choice.source not in _CV_SOURCES:
            continue
        identity = (str(Path(choice.path).resolve()), choice.sha256, choice.size_bytes)
        if identity not in checked:
            checked[identity] = (
                Path(choice.path).resolve().parent == root
                and verify_cv(choice)
                and _one_page(choice)
            )
        if not checked[identity]:
            continue
        if (choice.file, choice.sha256) not in groups:
            remember(choice.path, choice.sha256)
        group = groups.setdefault(
            (choice.file, choice.sha256),
            {
                "file": choice.file,
                "path": choice.path,
                "sha256": choice.sha256,
                "role_label": choice.role,
                "one_page_verified": True,
                "content_not_extracted": True,
                "job_count": 0,
                "outcome_counts": Counter(),
                "examples": [],
                "requires_fresh_job_review": True,
            },
        )
        group["job_count"] += 1
        group["outcome_counts"][job.status.value] += 1
        if len(group["examples"]) < examples_per_cv:
            group["examples"].append(
                {
                    "job_id": job.job_id,
                    "url": job.canonical_url,
                    "title": job.title,
                    "company": job.company,
                    "status": job.status.value,
                    "selection_source": choice.source,
                    "upload_event_ids": [
                        event["event_id"]
                        for event in activity
                        if _upload_binds_cv(event, job, choice)
                    ],
                }
            )
    cache.cv_routes = [groups[key] for key in sorted(groups)]
    cache.source_files = list(sources.values())
    if len({fact.key for fact in cache.facts}) != len(cache.facts):
        raise ValueError("A cached answer key must have one reviewed source")
    reject_secrets(cache.model_dump(mode="json"))
    cache._sources_verified = True
    return cache
