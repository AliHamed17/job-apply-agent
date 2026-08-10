"""Guarded, read-only Lever URL inspection.

This command exercises only navigation and sanitized form observation. It does
not fill answers, upload a CV, click submit, or create submission authority.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings, get_settings  # noqa: E402
from db.models import BrowserQualificationRun  # noqa: E402
from db.session import get_session_factory, init_db  # noqa: E402
from submitters.browser_trace import RedactedTrace  # noqa: E402
from submitters.lever_identity import LeverIdentityError, parse_lever_posting_identity  # noqa: E402
from submitters.lever_playwright import (  # noqa: E402
    PlaywrightLeverCandidateSession,
)
from submitters.lever_v1 import (  # noqa: E402
    LEVER_V1_SELECTOR_VERSION,
    LeverAdapterBlockedError,
    LeverPageState,
    assess_lever_v1_snapshot,
    lever_v1_final_action_binding,
    lever_v1_form_fingerprint,
    observe_lever_v1_fields,
)


def validate_smoke_guard(
    url: str,
    token: str | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    """Fail closed before opening a browser for any unsafe invocation."""

    active_settings = settings or get_settings()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.path.endswith("/apply"):
        raise RuntimeError("Exactly one explicit HTTPS Lever application URL is required.")
    try:
        identity = parse_lever_posting_identity(url)
    except LeverIdentityError as exc:
        raise RuntimeError("Exactly one explicit HTTPS Lever application URL is required.") from exc
    if identity.apply_url != url.split("#", 1)[0]:
        raise RuntimeError("Lever URL must be canonical and must not contain a fragment.")
    if not active_settings.dry_run:
        raise RuntimeError("Refusing smoke test: DRY_RUN=true is required.")
    if not active_settings.draft_only:
        raise RuntimeError("Refusing smoke test: DRAFT_ONLY=true is required.")
    if active_settings.portal_final_submit_enabled:
        raise RuntimeError("Refusing smoke test: final submission must remain disabled.")
    if active_settings.secret_key in {"", "change-me", "change-me-to-a-random-secret"}:
        raise RuntimeError("Refusing smoke test: configure a non-default operator secret.")
    supplied = token or os.environ.get("JOB_AGENT_OPERATOR_TOKEN", "")
    if not supplied or not hmac.compare_digest(supplied, active_settings.secret_key):
        raise RuntimeError("Refusing smoke test: operator authentication failed.")


def _write_inspection_report(path: str | Path, trace: RedactedTrace, *, qualified: bool) -> None:
    report = {
        "schema_version": "lever-selector-inspection-v1",
        "qualified": qualified,
        "mode": "read_only_form_inspection",
        "final_action_performed": False,
        "cv_uploaded": False,
        "events": trace.events,
        "privacy": (
            "No field answers, CV text, cookies, page content, names, URLs, "
            "emails, or phone numbers are retained."
        ),
    }
    Path(path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _persist_trace(trace: RedactedTrace, *, terminal_reason: str) -> None:
    init_db()
    db = get_session_factory()()
    try:
        db.add(
            BrowserQualificationRun(
                selector_version=LEVER_V1_SELECTOR_VERSION,
                terminal_reason=terminal_reason,
                qualified=False,
                trace_json=json.dumps(trace.events, separators=(",", ":")),
            )
        )
        db.commit()
    finally:
        db.close()


async def run_smoke(url: str, report_path: str) -> int:
    validate_smoke_guard(url)
    identity = parse_lever_posting_identity(url)
    trace = RedactedTrace(selector_version=LEVER_V1_SELECTOR_VERSION)
    session = PlaywrightLeverCandidateSession()
    terminal_reason = "INSPECTION_FAILED"
    qualified = False
    try:
        trace.record("navigation_started")
        await session.navigate(identity.apply_url)
        snapshot = await session.snapshot()
        assessment = assess_lever_v1_snapshot(
            snapshot.html,
            snapshot.url,
            identity=identity,
        )
        if assessment.state is LeverPageState.JOB:
            trace.record("candidate_form_opening")
            await session.open_candidate_form(identity)
            snapshot = await session.snapshot()
            assessment = assess_lever_v1_snapshot(
                snapshot.html,
                snapshot.url,
                identity=identity,
            )
        if assessment.reason_code is not None:
            terminal_reason = assessment.reason_code.value
            trace.record("terminal", terminal_reason=terminal_reason)
        elif assessment.state is not LeverPageState.FORM:
            terminal_reason = assessment.state.value.upper()
            trace.record("terminal", terminal_reason=terminal_reason)
        else:
            fields = observe_lever_v1_fields(snapshot.html, identity=identity)
            binding = lever_v1_final_action_binding(
                snapshot.html,
                identity=identity,
                fields=fields,
            )
            fingerprint = lever_v1_form_fingerprint(identity, fields, binding)
            trace.record(
                "form_observed",
                field_types=sorted({field.field_type.value for field in fields}),
                step=len(fields),
            )
            terminal_reason = "READ_ONLY_FORM_OBSERVED"
            trace.record("terminal", terminal_reason=terminal_reason)
            qualified = bool(fingerprint) and session._final_request_count == 0
    except (LeverAdapterBlockedError, LeverIdentityError) as exc:
        reason = getattr(getattr(exc, "reason_code", None), "value", "INSPECTION_FAILED")
        terminal_reason = str(reason)
        trace.record("terminal", terminal_reason=terminal_reason)
    except Exception:
        trace.record("terminal", terminal_reason=terminal_reason)
    finally:
        await session.close()
        _write_inspection_report(report_path, trace, qualified=qualified)
        _persist_trace(trace, terminal_reason=terminal_reason)
    return 0 if qualified else 2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--report", default="lever-selector-inspection.json")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_smoke(args.url, args.report)))
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
