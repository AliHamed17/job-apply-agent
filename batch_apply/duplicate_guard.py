"""Cross-platform duplicate guard for the browser-batch ledger.

The ledger identifies a job by its canonical URL, so the same posting reached
through two sites (a company's Comeet page and its LinkedIn Easy Apply copy)
looks like two jobs. This read-only guard compares a normalized employer and
title against jobs that were already submitted, are uncertain, or are being
worked on by another lane. A match is a *probable* duplicate: two real
requisitions can share a title, so the caller may record a reviewed override.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

_BLOCKING = ("submitted", "already_applied", "submission_uncertain", "in_progress")
_LEGAL = re.compile(r"\b(ltd|inc|llc|gmbh|corp|corporation|co|company)\b\.?|בע\"?מ", re.I)
_GENDER = re.compile(r"\(\s*[mfwd]\s*/\s*[mfwd]([^)]*)\)|\s*/\s*ת\b|\(\s*ית\s*\)", re.I)
_PAREN = re.compile(r"\([^)]*\)")


class ProbableDuplicateError(ValueError):
    """The same employer and title already has an application in the ledger."""


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.casefold(), flags=re.UNICODE).strip()


def normalize_identity(company: str, title: str) -> str:
    """Stable key from employer and title; empty when either is unknown."""
    firm = _squash(_LEGAL.sub(" ", company or ""))
    role = _squash(_PAREN.sub(" ", _GENDER.sub(" ", title or "")))
    return f"{firm}|{role}" if firm and role else ""


def find_probable_duplicates(
    ledger_path: str | Path, company: str, title: str, *, exclude_job_id: str = ""
) -> list[dict]:
    """Return other ledger jobs with the same normalized employer and title."""
    key = normalize_identity(company, title)
    if not key:
        return []
    uri = f"file:{Path(ledger_path).resolve().as_posix()}?mode=ro"
    matches = []
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT job_id, canonical_url, status, payload FROM browser_batch_jobs"
        ).fetchall()
    for job_id, url, status, payload in rows:
        if job_id == exclude_job_id or status not in _BLOCKING:
            continue
        data = json.loads(payload)
        if normalize_identity(data.get("company", ""), data.get("title", "")) == key:
            matches.append(
                {"job_id": job_id, "status": status, "canonical_url": url, "title": data["title"]}
            )
    return matches


def require_not_probable_duplicate(
    ledger_path: str | Path,
    company: str,
    title: str,
    *,
    job_id: str = "",
    reviewed_distinct: bool = False,
) -> list[dict]:
    """Raise before Submit when the same employer and title is already applied to.

    ``reviewed_distinct`` is for a requisition the operator or LLM has compared
    with the earlier one and found to be a different opening.
    """
    matches = find_probable_duplicates(ledger_path, company, title, exclude_job_id=job_id)
    if matches and not reviewed_distinct:
        first = matches[0]
        raise ProbableDuplicateError(
            f"{company} / {title} already has a {first['status']} application "
            f"({first['canonical_url']}); confirm a different requisition or skip"
        )
    return matches
