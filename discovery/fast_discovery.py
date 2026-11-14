"""Read-only, bounded discovery of saved URLs using the existing HTML parsers.

This module has no browser, application, task-queue, or database side effects.
Only known public ATS hosts (or explicitly opted-in company hosts) receive GETs.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from batch_apply.urls import canonical_job_url
from ingestion.url_utils import extract_urls
from jobs.extractor import extract_jobs

_ATS_HOSTS = {
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "jobs.ashbyhq.com",
    "comeet.com",
    "www.comeet.com",
    "comeet.co",
    "www.comeet.co",
}
_BROWSER_DOMAINS = {"linkedin.com", "indeed.com", "indeed.co.uk", "indeed.co.il"}
_SENSITIVE_PARAMS = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "code",
    "session",
    "sessionid",
    "password",
    "secret",
    "signature",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "email",
}
_URL_FIELDS = {"url", "job_url", "canonical_url", "source_url", "apply_url", "provider_url"}
_TEXT_FIELDS = {"text", "source_text", "body", "message", "caption"}
_CONTAINER_FIELDS = {"leads", "tabs", "jobs", "messages", "urls", "events"}
_AUTH_PATHS = {"login", "signin", "sign-in", "signup", "register", "oauth", "checkpoint"}


def read_saved_urls(text: str) -> list[str]:
    """Read text exports or saved lead/tab/message JSON without retaining message text."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return extract_urls(text)
    urls: list[str] = []

    def visit(value):
        if isinstance(value, str):
            urls.extend(extract_urls(value))
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in _URL_FIELDS | _TEXT_FIELDS | _CONTAINER_FIELDS:
                    visit(item)

    visit(data)
    return list(dict.fromkeys(urls))


def _domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _canonical(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("https_required")
    if parsed.username or parsed.password:
        raise ValueError("credentials_in_url")
    if parsed.port not in {None, 443}:
        raise ValueError("nonstandard_port")
    host = parsed.hostname.lower()
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("nonpublic_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("nonpublic_host")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() in _SENSITIVE_PARAMS or key.lower().endswith("_token") for key, _ in query):
        raise ValueError("sensitive_query")
    if _domain(host, "linkedin.com"):
        parsed = parsed._replace(netloc="www.linkedin.com")
    elif any(_domain(host, domain) for domain in _BROWSER_DOMAINS - {"linkedin.com"}):
        job_id = next((value for key, value in query if key.lower() == "jk"), "")
        if job_id:
            return "https://www.indeed.com/viewjob?" + urlencode({"jk": job_id})
    return canonical_job_url(urlunsplit(parsed))


def _result(url: str, status: str, reason: str, **extra) -> dict:
    return {
        "url": url,
        "status": status,
        "reason": reason,
        "jobs": [],
        "cache_hit": False,
        "revalidated": False,
        "steps": [],
        **extra,
    }


def write_json_atomic(path: Path, data: dict) -> None:
    """Replace a local manifest/cache only after the new JSON is fully written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class DiscoveryBatch:
    """Concurrent discovery with persistent TTL + conditional-GET caching.

    Instances and cache files have a single owner. Separate workers should use
    separate cache paths. The injected client must not follow redirects or have
    authentication/session defaults; production calls create a clean client.
    """

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        client: httpx.AsyncClient | None = None,
        concurrency: int = 4,
        per_host: int = 2,
        ttl_seconds: float = 21600,
        allowed_hosts: list[str] | tuple[str, ...] = (),
        max_bytes: int = 2_000_000,
        timeout_seconds: float = 15,
    ):
        if not 1 <= concurrency <= 16 or not 1 <= per_host <= concurrency:
            raise ValueError("Require 1 <= per_host <= concurrency <= 16")
        if ttl_seconds < 0 or max_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("TTL must be nonnegative; size and timeout must be positive")
        self.allowed_hosts = {host.lower().strip() for host in allowed_hosts}
        if any(not host or any(char in host for char in "/:@*") for host in self.allowed_hosts):
            raise ValueError("Opt-in hosts must be exact hostnames, without URLs or wildcards")
        self.cache_path = Path(cache_path) if cache_path else None
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self._global = asyncio.Semaphore(concurrency)
        self._per_host = per_host
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._dns: dict[str, bool] = {}
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        if self.cache_path:
            try:
                saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if saved.get("version") == 1 and isinstance(saved.get("entries"), dict):
                    return saved["entries"]
            except (OSError, ValueError, AttributeError):
                pass
        return {}

    def _policy(self, url: str) -> str:
        host = urlsplit(url).hostname or ""
        if any(_domain(host, domain) for domain in _BROWSER_DOMAINS):
            return "signed_in_browser_required"
        if any(part.lower() in _AUTH_PATHS for part in urlsplit(url).path.split("/")):
            return "authentication_route"
        if host not in _ATS_HOSTS and host not in self.allowed_hosts:
            return "host_requires_opt_in"
        return ""

    async def _public_dns(self, host: str) -> bool:
        if host not in self._dns:
            try:
                addresses = await asyncio.wait_for(
                    asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM),
                    timeout=self.timeout_seconds,
                )
                self._dns[host] = bool(addresses) and all(
                    ipaddress.ip_address(row[4][0]).is_global for row in addresses
                )
            except (OSError, ValueError, TimeoutError):
                self._dns[host] = False
        return self._dns[host]

    async def run(self, urls: list[str]) -> dict:
        """Return an ordered review manifest; no job is claimed, applied to, or submitted."""
        if len(urls) > 500:
            raise ValueError("Split inputs into batches of at most 500 URLs")
        unique: dict[str, None] = {}
        rejected: list[dict] = []
        for raw in urls:
            try:
                unique[_canonical(raw)] = None
            except (ValueError, TypeError, AttributeError):
                # Never persist credentials, auth query values, or raw invalid inputs.
                rejected.append(_result("", "rejected", "unsafe_or_invalid_url"))
        owned_client = self.client is None
        if owned_client:
            self.client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "JobApplyAgent-Discovery/0.1 (read-only)"},
            )
        try:
            results = await asyncio.gather(*(self._discover(url) for url in unique))
        finally:
            if owned_client:
                await self.client.aclose()
                self.client = None
            if self.cache_path:
                write_json_atomic(self.cache_path, {"version": 1, "entries": self._cache})
        return {
            "version": 1,
            "generated_at": time.time(),
            "input_count": len(urls),
            "duplicate_count": len(urls) - len(unique) - len(rejected),
            "results": results + rejected,
        }

    async def _discover(self, url: str) -> dict:
        # Check current policy before cache: an old opt-in cannot authorize a new run.
        reason = self._policy(url)
        if reason:
            return _result(url, "browser_handoff", reason)
        old = self._cache.get(url, {})
        valid_old = (
            isinstance(old, dict)
            and isinstance(old.get("fetched_at"), (int, float))
            and isinstance(old.get("result"), dict)
            and old["result"].get("url") == url
            and old["result"].get("status") in {"discovered", "browser_handoff", "unavailable"}
        )
        if not valid_old:
            old = {}
        ttl = self.ttl_seconds if old.get("result", {}).get("status") == "discovered" else 300
        if old and 0 <= time.time() - old["fetched_at"] < min(self.ttl_seconds, ttl):
            result = {**old["result"], "cache_hit": True}
            result["steps"] = [*result.get("steps", []), {"action": "cache_hit"}]
            return result
        try:
            result, validators = await self._fetch(url, old)
        except (httpx.HTTPError, OSError, TimeoutError):
            return _result(url, "error", "network_error")
        if result["status"] in {"discovered", "browser_handoff", "unavailable"}:
            self._cache[url] = {"fetched_at": time.time(), "result": result, **validators}
        return result

    async def _fetch(self, url: str, old: dict) -> tuple[dict, dict]:
        current = url
        steps = []
        validators = {}
        for _ in range(4):
            host = urlsplit(current).hostname or ""
            host_limit = self._hosts.setdefault(host, asyncio.Semaphore(self._per_host))
            async with host_limit, self._global:
                if not await self._public_dns(host):
                    return _result(url, "rejected", "nonpublic_or_unresolved_host"), {}
                headers = {}
                # Validators belong to the exact fetched URL, not a redirect destination.
                if (
                    current == old.get("fetched_url")
                    and old.get("result", {}).get("status") == "discovered"
                ):
                    if old.get("etag"):
                        headers["If-None-Match"] = old["etag"]
                    if old.get("last_modified"):
                        headers["If-Modified-Since"] = old["last_modified"]
                async with self.client.stream(
                    "GET",
                    current,
                    headers=headers,
                    follow_redirects=False,
                    timeout=self.timeout_seconds,
                ) as response:
                    status = response.status_code
                    steps.append({"action": "get", "url": current, "http_status": status})
                    if status in {301, 302, 303, 307, 308}:
                        try:
                            target = _canonical(
                                urljoin(current, response.headers.get("location", ""))
                            )
                        except ValueError:
                            return _result(url, "rejected", "unsafe_redirect", steps=steps), {}
                        reason = self._policy(target)
                        if reason:
                            return _result(
                                url,
                                "browser_handoff",
                                reason,
                                steps=[*steps, {"action": "browser_handoff", "url": target}],
                            ), {}
                        current = target
                        continue
                    if status == 304 and old.get("result", {}).get("status") == "discovered":
                        result = {
                            **old["result"],
                            "cache_hit": False,
                            "revalidated": True,
                            "steps": [*steps, {"action": "not_modified"}],
                        }
                        return result, {
                            key: old[key]
                            for key in ("etag", "last_modified", "fetched_url")
                            if key in old
                        }
                    if status in {401, 403, 429}:
                        return _result(url, "browser_handoff", f"http_{status}", steps=steps), {}
                    if status in {404, 410}:
                        return _result(url, "unavailable", f"http_{status}", steps=steps), {}
                    if not 200 <= status < 300:
                        return _result(url, "error", f"http_{status}", steps=steps), {}
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            return _result(
                                url, "browser_handoff", "response_too_large", steps=steps
                            ), {}
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    validators = {
                        "etag": response.headers.get("etag", ""),
                        "last_modified": response.headers.get("last-modified", ""),
                        "fetched_url": current,
                    }
            digest = hashlib.sha256(content).hexdigest()
            if (
                current == old.get("fetched_url")
                and old.get("result", {}).get("status") == "discovered"
                and old["result"].get("content_sha256") == digest
            ):
                return {
                    **old["result"],
                    "cache_hit": False,
                    "revalidated": True,
                    "steps": [*steps, {"action": "content_unchanged"}],
                }, validators
            html = content.decode(encoding, errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            for hidden in soup(["script", "style"]):
                hidden.decompose()
            visible = soup.get_text(" ", strip=True).lower()[:800]
            challenge = any(
                marker in visible
                for marker in (
                    "verify you are human",
                    "checking your browser",
                    "access denied",
                    "just a moment",
                    "complete the security check",
                )
            )
            if challenge:
                return _result(url, "browser_handoff", "challenge_page", steps=steps), {}
            parsed = extract_jobs(html, current)
            jobs = []
            seen = set()
            for job in parsed.jobs:
                if not job.is_complete or (
                    not job.description.strip() and parsed.page_type != "listing"
                ):
                    continue
                data = job.model_dump()
                data["source_url"] = current
                try:
                    data["apply_url"] = _canonical(job.apply_url or current)
                except ValueError:
                    continue
                if data["apply_url"] in seen:
                    continue
                seen.add(data["apply_url"])
                data["detail_required"] = not bool(job.description.strip())
                jobs.append(data)
            if jobs:
                return _result(
                    url,
                    "discovered",
                    "public_postings_extracted",
                    jobs=jobs,
                    parser=parsed.parser_used,
                    content_sha256=digest,
                    steps=[
                        *steps,
                        {"action": "parse", "parser": parsed.parser_used, "job_count": len(jobs)},
                    ],
                ), validators
            return _result(url, "browser_handoff", "job_details_require_browser", steps=steps), {}
        return _result(url, "browser_handoff", "redirect_limit", steps=steps), {}
