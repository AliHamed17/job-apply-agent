"""WhatsApp/email outbound applier orchestration (Task 5.5).

Consumes a text-only WhatsApp post (a recruiter "hiring" broadcast with no
job-board URL), parses it into a job, scores it against the profile, and — if
it clears the bar — messages the recruiter back on whichever channel they
posted a contact for (WhatsApp DM preferred, email fallback), attaching the
CV. Gated by score, a governor-enforced daily send cap, and per-contact
dedup so we never spam or exceed budget.

All IO/LLM calls are injected via ``deps`` (a ``SimpleNamespace`` bundling
``parse``, ``bridge``, ``email``, ``gen_msg`` and ``now``) so tests can run
fully offline against fakes + an in-memory DB.
"""

from __future__ import annotations

import hashlib
import json

import structlog

from db.models import Job, JobStatus, Message
from ingestion.text_post_parser import ParsedPost, looks_like_job  # noqa: F401 (re-exported)
from ingestion.url_utils import job_signature
from jobs.models import JobData
from match.scoring import Action, decide_action, score_job
from worker.outbound_dedup import can_contact

logger = structlog.get_logger(__name__)

__all__ = ["ParsedPost", "looks_like_job", "process_text_post"]


def _text_post_digest(text: str, sender: str | None) -> str:
    """Create a stable local identity for one text post without exposing it."""

    value = f"{sender or 'whatsapp-text'}\n{text}".encode()
    return hashlib.sha256(value).hexdigest()


def _persist_text_post_job(
    db,
    *,
    text: str,
    sender: str | None,
    job: JobData,
    score: float,
    skip_reason: str | None,
) -> Job:
    """Persist a parsed text-only post so it cannot disappear at a safety gate.

    Text posts do not have an employer URL, so they are deliberately recorded
    as scored, non-Easy-Apply jobs.  The existing outbound permit gate still
    decides whether any recruiter message could be sent.  Repeated bridge
    delivery is idempotent by the normal job signature and a deterministic
    local message id; raw text remains in the private database only.
    """

    signature = job_signature(job.title, job.company, job.location)
    existing = db.query(Job).filter(Job.job_signature == signature).first()
    if existing is not None:
        return existing

    digest = _text_post_digest(text, sender)
    message = (
        db.query(Message)
        .filter(Message.whatsapp_message_id == f"whatsapp-text-{digest[:32]}")
        .first()
    )
    if message is None:
        message = Message(
            whatsapp_message_id=f"whatsapp-text-{digest[:32]}",
            sender_phone=sender or "whatsapp-text",
            body=text,
        )
        db.add(message)
        db.flush()

    db_job = Job(
        extracted_url_id=None,
        title=job.title.strip(),
        company=job.company.strip(),
        location=job.location.strip(),
        employment_type=job.employment_type.strip(),
        seniority=job.seniority.strip(),
        description=job.description.strip(),
        requirements=job.requirements.strip(),
        apply_url="",
        source_url="",
        date_posted=job.date_posted.strip(),
        keywords=json.dumps(job.keywords),
        apply_url_hash=None,
        job_signature=signature,
        status=JobStatus.SKIPPED if skip_reason else JobStatus.SCORED,
        score=score,
        discovery_source="whatsapp_text",
        easy_apply=False,
    )
    db.add(db_job)
    db.flush()
    logger.info(
        "whatsapp_text_job_persisted",
        job_id=db_job.id,
        score=round(score, 1),
        state=db_job.status.value,
    )
    return db_job


async def process_text_post(text, db, settings, profile, governor, deps, sender=None) -> str:
    """Parse, score, and (maybe) reply to a text-only job post.

    ``sender`` is the poster's own WhatsApp number (supplied by the bridge for
    group posts). It's used as the contact of last resort for "DM me" style
    posts that carry no phone/email in the body.

    Returns one of: "not_job" | "low_score" | "duplicate" | "capped" |
    "draft_only" | "permit_required" | "no_contact". Legacy sent outcomes
    remain unreachable until a one-use final-submit permit is implemented.
    """
    parsed = await deps.parse(text)
    if not parsed.is_job:
        return "not_job"

    job = JobData(
        title=parsed.title,
        company=parsed.company,
        description=parsed.description,
    )

    breakdown = score_job(job, profile)
    # Persist the parsed posting before any governor, contact, draft-only, or
    # permit decision.  Previously a text-only WhatsApp job was scored and
    # then discarded at ``DRAFT_ONLY``/``permit_required``, leaving no record
    # in the dashboard for the operator to review.
    _persist_text_post_job(
        db,
        text=text,
        sender=sender,
        job=job,
        score=breakdown.total,
        skip_reason=breakdown.skip_reason
        or ("LOW_SCORE" if breakdown.total < settings.min_apply_score else None),
    )
    action = decide_action(
        score=breakdown.total,
        auto_apply_enabled=settings.auto_apply,
        draft_only=settings.draft_only,
        skip_reason=breakdown.skip_reason,
        min_apply_score=settings.min_apply_score,
    )

    if action != Action.AUTO_APPLY and breakdown.total < settings.min_apply_score:
        logger.info("outbound_low_score", title=job.title, score=breakdown.total)
        return "low_score"

    ok, reason = governor.can_act()
    if not ok or governor.wa_remaining() <= 0:
        logger.info("outbound_capped", title=job.title, reason=reason)
        return "capped"

    if parsed.contact_phone:
        channel = "whatsapp"
        contact_value = parsed.contact_phone
    elif parsed.contact_email:
        channel = "email"
        contact_value = parsed.contact_email
    elif sender:
        # No phone/email in the post body — fall back to the poster's own
        # WhatsApp number (e.g. "interested? DM me"). This is the only
        # usable contact for such posts.
        channel = "whatsapp"
        contact_value = sender
    else:
        return "no_contact"

    now = deps.now
    if not can_contact(db, contact_value, settings.wa_contact_dedup_days, now):
        logger.info("outbound_duplicate", contact=contact_value)
        return "duplicate"

    # DRAFT_ONLY is the master switch — never DM/email a recruiter on the
    # user's behalf while it's on. Gated here (not earlier) so scoring,
    # the governor cap, and dedup all still run and log normally; only
    # the actual send (and its bookkeeping) is skipped.
    if settings.draft_only:
        logger.info("outbound_draft_only", title=job.title, contact=contact_value)
        return "draft_only"

    # Recruiter DMs and emails are external application actions too. The
    # legacy score/governor gates are not operator consent, so this path stays
    # hard-disabled until the same one-use submit permit used by ATS adapters
    # is available.
    logger.info("outbound_submit_permit_required", title=job.title, channel=channel)
    return "permit_required"
