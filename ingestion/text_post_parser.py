"""Classify + extract job info from text-only WhatsApp posts."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

_KEYWORDS = (
    "hiring",
    "vacancy",
    "vacancies",
    "send cv",
    "send resume",
    "looking for",
    "we are recruiting",
    "job opening",
    "apply",
    "position",
    "مطلوب",
    "توظيف",
    "وظيفة",
    "شاغر",
)  # Arabic: required / hiring / job / vacancy


def looks_like_job(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _KEYWORDS)


@dataclass
class ParsedPost:
    is_job: bool = False
    title: str = ""
    company: str = ""
    description: str = ""
    contact_phone: str = ""
    contact_email: str = ""


_PROMPT = """Decide if this WhatsApp message is a job posting. If yes, extract fields.
Return ONLY JSON: {{"is_job": bool, "title": "", "company": "", "description": "",
"contact_phone": "", "contact_email": ""}}. Use "" for anything absent. Do not invent contacts.

MESSAGE:
{text}
"""


async def parse_text_post(text: str, client: LLMClient | None = None) -> ParsedPost:
    if not looks_like_job(text):
        return ParsedPost(is_job=False)
    client = client or get_llm_client()
    try:
        raw = await client.generate_json(prompt=_PROMPT.format(text=text[:2000]))
    except Exception as exc:
        logger.warning("text_post_parse_failed", error=str(exc))
        return ParsedPost(is_job=False)

    # Provider JSON is untrusted, even when the transport advertises a JSON
    # response.  Keep the outbound path fail-closed: a non-object, non-boolean
    # classification, or non-string field is not a usable job post.
    if not isinstance(raw, dict) or not isinstance(raw.get("is_job"), bool):
        logger.warning("text_post_parse_invalid_shape")
        return ParsedPost(is_job=False)
    if not raw["is_job"]:
        return ParsedPost(is_job=False)

    def _text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    title = _text(raw.get("title"))
    description = _text(raw.get("description"))
    if not title or not description:
        logger.warning("text_post_parse_incomplete_job")
        return ParsedPost(is_job=False)

    return ParsedPost(
        is_job=True,
        title=title,
        company=_text(raw.get("company")),
        description=description,
        contact_phone=_text(raw.get("contact_phone")),
        contact_email=_text(raw.get("contact_email")),
    )
