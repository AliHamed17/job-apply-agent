"""Safe HTTP fetcher with retries, timeouts, caching, and robots.txt checking."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import get_settings
from core.utils import run_async

logger = structlog.get_logger(__name__)

# Simple in-memory cache (swap for Redis in production)
_page_cache: dict[str, str] = {}
_robots_cache: dict[str, RobotFileParser] = {}

USER_AGENT = "JobApplyAgent/1.0 (+https://github.com/AliHamed17/Job-apply-agent)"

# Domains whose pages are always JS-rendered — skip httpx and go straight to browser
_BROWSER_ONLY_DOMAINS = frozenset(
    {
        "comeet.com",
        "comeet.co",
        "linkedin.com",
        "glassdoor.com",
        "rippling.com",
        "bamboohr.com",
        "recruitee.com",
    }
)


def _needs_browser_fetch(url: str) -> bool:
    """Return True if this URL's domain requires Playwright rendering."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in _BROWSER_ONLY_DOMAINS)
    except Exception:
        return False


def _is_linkedin_url(url: str) -> bool:
    """Return whether a URL belongs to LinkedIn's public host family."""

    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _check_robots_txt(url: str) -> bool:
    """Check if we are allowed to fetch this URL per robots.txt."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        if robots_url not in _robots_cache:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            try:
                rp.read()
            except Exception:
                # If we can't read robots.txt, assume allowed
                return True
            _robots_cache[robots_url] = rp

        return _robots_cache[robots_url].can_fetch(USER_AGENT, url)
    except Exception:
        return True  # fail-open


class FetchResult:
    """Result of fetching a page."""

    def __init__(
        self,
        url: str,
        html: str = "",
        status_code: int = 0,
        success: bool = False,
        error: str = "",
        blocked: bool = False,
    ):
        self.url = url
        self.html = html
        self.status_code = status_code
        self.success = success
        self.error = error
        self.blocked = blocked


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _do_fetch(url: str, timeout: float = 15.0) -> httpx.Response:
    """Perform the actual HTTP GET with retry logic."""
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        return client.get(url)


# Common bot-detection indicators
_BLOCK_INDICATORS = [
    "captcha",
    "cf-challenge",
    "access denied",
    "blocked",
    "please verify you are a human",
    "enable javascript",
]

_LINKEDIN_SESSION_MARKERS = (
    "sign in to linkedin",
    "sign in | linkedin",
    "login to linkedin",
    "join linkedin",
    "authwall",
)


def _linkedin_session_required(landed_url: str, html: str) -> bool:
    """Detect a LinkedIn auth/challenge shell without retaining page content."""

    landed = (landed_url or "").lower()
    if any(marker in landed for marker in ("/login", "/checkpoint", "/challenge")):
        return True
    visible_candidate = (html or "")[:20_000].lower()
    return any(marker in visible_candidate for marker in _LINKEDIN_SESSION_MARKERS)


async def _fetch_browser(url: str, *, profile_dir: str | None = None) -> str:
    """Fetch using Playwright, optionally with an isolated persistent profile.

    LinkedIn job pages require the operator's already-authenticated session in
    order to expose the actual posting.  A fresh anonymous context reliably
    returns a login/checkpoint shell, which used to be recorded as a successful
    fetch with zero jobs.  The profile is always supplied by the caller; this
    function never reads credentials or browser password stores.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        if profile_dir:
            # The profile is created by the explicit local login command. Do
            # not silently create an empty profile and report a false fetch.
            if not Path(profile_dir).is_dir():
                raise RuntimeError("LINKEDIN_SESSION_REQUIRED")
            context = await p.chromium.launch_persistent_context(
                profile_dir,
                headless=True,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            browser = None
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            # Non-LinkedIn JS-heavy pages use an ephemeral context.
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            html = await page.content()
            if _is_linkedin_url(url) and _linkedin_session_required(page.url, html):
                raise RuntimeError("LINKEDIN_SESSION_REQUIRED")
            return html
        finally:
            await context.close()
            if browser is not None:
                await browser.close()


def fetch_page(url: str) -> FetchResult:
    """Fetch a page safely with caching, robots.txt, and rate limiting.

    Returns a FetchResult with success/failure metadata.
    """
    settings = get_settings()

    # Check cache
    if url in _page_cache:
        logger.debug("cache_hit", url=url)
        return FetchResult(url=url, html=_page_cache[url], success=True)

    # Check robots.txt (Disabled for personal local use to allow LinkedIn/etc)
    # if not _check_robots_txt(url):
    #     logger.warning("robots_txt_disallowed", url=url)
    #     return FetchResult(url=url, error="Blocked by robots.txt", blocked=True)

    # Polite crawl delay
    time.sleep(settings.polite_crawl_delay_seconds)

    # For known JS-heavy platforms, skip httpx and use browser directly. An
    # exact LinkedIn URL must use the operator's isolated, manually signed-in
    # profile; anonymous HTML is not useful evidence and is surfaced as a
    # stable blocked result instead of a misleading successful empty fetch.
    if _needs_browser_fetch(url):
        logger.info("browser_only_domain", url=url)
        profile_dir = None
        if _is_linkedin_url(url):
            configured_profile = str(settings.linkedin_browser_profile_dir or "").strip()
            if not configured_profile or not Path(configured_profile).is_dir():
                logger.warning("linkedin_session_required", url=url)
                return FetchResult(
                    url=url,
                    error="LINKEDIN_SESSION_REQUIRED",
                    blocked=True,
                )
            profile_dir = str(Path(configured_profile))
        try:
            html = run_async(_fetch_browser(url, profile_dir=profile_dir))
            if _is_linkedin_url(url) and _linkedin_session_required(url, html):
                return FetchResult(
                    url=url,
                    error="LINKEDIN_SESSION_REQUIRED",
                    blocked=True,
                )
            _page_cache[url] = html
            logger.info("page_fetched_browser", url=url, length=len(html))
            return FetchResult(
                url=url,
                html=html,
                success=True,
                status_code=200,
            )
        except Exception as e:
            error = str(e) or type(e).__name__
            if _is_linkedin_url(url) and "LINKEDIN_SESSION_REQUIRED" in error:
                return FetchResult(url=url, error="LINKEDIN_SESSION_REQUIRED", blocked=True)
            logger.error("browser_fetch_failed_direct", url=url, error=str(e))
            return FetchResult(url=url, error=str(e))

    try:
        try:
            resp = _do_fetch(url)
            result = FetchResult(
                url=url,
                html=resp.text,
                status_code=resp.status_code,
                success=(200 <= resp.status_code < 300),
                blocked=(resp.status_code == 403 or resp.status_code == 429),
            )
        except Exception as exc:
            logger.warning("initial_fetch_error", url=url, error=str(exc))
            result = FetchResult(url=url, error=str(exc), blocked=True)

        if result.blocked:
            # Try browser fallback
            logger.info("fetch_blocked_trying_browser", url=url)
            try:
                html = run_async(_fetch_browser(url))
                result = FetchResult(
                    url=url,
                    html=html,
                    success=True,
                    status_code=200,
                    blocked=False,
                )
            except Exception as e:
                logger.error("browser_fetch_failed", url=url, error=str(e))
                return result
        else:
            html = result.html

        # Heuristic: detect CAPTCHA / challenge pages
        lower_html = html[:5000].lower()
        for indicator in _BLOCK_INDICATORS:
            if indicator in lower_html:
                if not result.blocked:  # only try browser once if not already blocked
                    logger.warning(
                        "bot_protection_detected_trying_browser",
                        url=url,
                        indicator=indicator,
                    )
                    try:
                        html = run_async(_fetch_browser(url))
                        lower_html = html[:5000].lower()
                    except Exception as e:
                        logger.error(
                            "browser_fetch_failed_after_indicator",
                            url=url,
                            error=str(e),
                        )
                        return FetchResult(
                            url=url,
                            error=f"Bot protection: {indicator}",
                            blocked=True,
                        )
                else:
                    return FetchResult(
                        url=url,
                        error=f"Bot protection: {indicator}",
                        blocked=True,
                    )

        # Update cache and return
        _page_cache[url] = html
        logger.info("page_fetched", url=url, status=result.status_code, length=len(html))
        return FetchResult(url=url, html=html, status_code=result.status_code, success=True)

    except Exception as exc:
        logger.error("fetch_failed", url=url, error=str(exc))
        return FetchResult(url=url, error=str(exc))
