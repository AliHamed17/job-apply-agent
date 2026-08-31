"""Run local discovery on a schedule without requiring Redis or Celery Beat.

This convenience runner is intentionally qualification-only. It refuses to
start unless both DRY_RUN and DRAFT_ONLY are enabled.
"""

from __future__ import annotations

import signal
import threading
from profile.loader import get_profile
from profile.models import UserProfile
from profile.readiness import profile_discovery_readiness_issues

import structlog

from core.config import Settings, get_settings
from worker.discovery_tasks import discover_jobs_task

logger = structlog.get_logger(__name__)
_stop = threading.Event()


def validate_safe_mode(settings: Settings) -> None:
    if not settings.dry_run or not settings.draft_only:
        raise RuntimeError("Safe automation requires DRY_RUN=true and DRAFT_ONLY=true")


def validate_discovery_profile(profile: UserProfile) -> None:
    """Require only the profile facts needed to discover jobs.

    Discovery is intentionally independent from candidate identity.  Reusing
    the full preparation readiness check here made the local runner refuse to
    start until a name, email, phone, legal facts, and resume had been
    onboarded—even though discovery itself never sends anything employer
    facing.  Preparation and submission keep their stricter gates in the API
    and worker paths.
    """

    issues = profile_discovery_readiness_issues(profile)
    if issues:
        raise RuntimeError("Profile is not ready for discovery: " + ", ".join(issues))


def _request_stop(_signum, _frame) -> None:
    _stop.set()


def main() -> int:
    settings = get_settings()
    validate_safe_mode(settings)
    validate_discovery_profile(get_profile())
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    interval_seconds = max(1, settings.discovery_interval_h) * 3600
    logger.info(
        "safe_automation_started",
        interval_seconds=interval_seconds,
        final_submission_enabled=False,
    )

    while not _stop.is_set():
        try:
            inserted = discover_jobs_task()
            logger.info("safe_automation_cycle_complete", inserted=inserted)
        except Exception:
            logger.exception("safe_automation_cycle_failed")
        _stop.wait(interval_seconds)
    logger.info("safe_automation_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
