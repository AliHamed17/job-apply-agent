"""Conservative, offline canonicalization for duplicate open job tabs."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = {
    "trk",
    "trkemail",
    "trackingid",
    "refid",
    "referralid",
    "fbclid",
    "gclid",
    "gh_src",
    "lever-origin",
    "mc_cid",
    "mc_eid",
    "originalsubdomain",
}
_LEVER_JOB_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
_GREENHOUSE_JOB_HOSTS = {
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
}
_COMEET_JOB_HOSTS = {"comeet.com", "www.comeet.com", "comeet.co", "www.comeet.co"}
_LEVER_POSTING_SUFFIX = re.compile(
    r"(/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"/(?:apply|thanks)$",
    re.IGNORECASE,
)
_GREENHOUSE_POSTING_SUFFIX = re.compile(r"(/[^/]+/jobs/\d+)/confirmation$")


def canonical_job_url(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A job tab must have an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in a job URL")
    hostname = parsed.hostname.lower()
    host = hostname
    port = parsed.port
    if ":" in host:
        host = f"[{host}]"
    if port and (parsed.scheme.lower(), port) not in {("https", 443), ("http", 80)}:
        host += f":{port}"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    path = parsed.path.rstrip("/") or "/"
    # LinkedIn can expose a selected posting inside a search/list page.
    if hostname in {"linkedin.com", "www.linkedin.com"}:
        job_id = next((v for k, v in query if k.lower() == "currentjobid"), "")
        match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)$", path)
        if match:
            job_id = match.group(1)
        if job_id.isdigit():
            return f"https://www.linkedin.com/jobs/view/{job_id}"
    # These provider-specific form/success routes retain the posting identity.
    # Keep other suffixes/hosts unchanged: an arbitrary /apply is not proof that
    # its parent URL is the same job. Neither suffix implies an application state.
    if hostname in _LEVER_JOB_HOSTS:
        match = _LEVER_POSTING_SUFFIX.fullmatch(path)
        if match:
            path = match.group(1)
    elif hostname in _GREENHOUSE_JOB_HOSTS:
        match = _GREENHOUSE_POSTING_SUFFIX.fullmatch(path)
        if match:
            path = match.group(1)
    # Comeet's parser identifies a job entirely from /jobs/... path segments;
    # coref is the referral value and does not participate in posting identity.
    comeet_job = hostname in _COMEET_JOB_HOSTS and path.startswith("/jobs/")
    clean_query = sorted(
        (key, value)
        for key, value in query
        if key.lower() not in _TRACKING
        and not key.lower().startswith("utm_")
        and not (comeet_job and key.lower() == "coref")
    )
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(clean_query), ""))


def job_key(url: str) -> str:
    return hashlib.sha256(canonical_job_url(url).encode("utf-8")).hexdigest()[:24]
