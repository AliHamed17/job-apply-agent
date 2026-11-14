"""No-question field decisions that preserve truthful profile evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from submitters.form_brain import AnswerResult, FieldSpec

_DEMOGRAPHIC = re.compile(r"gender|race|ethnic|veteran|disabil|demographic", re.I)
_DECLINE = re.compile(r"prefer\s+not|decline\s+to|do\s+not\s+(?:wish|want)\s+to", re.I)
_SUPPLEMENTARY = re.compile(
    r"^(?:additional information|additional comments|comments|"
    r"anything else(?: you(?: would)? like (?:us )?to know)?)[?.:\s]*$",
    re.I,
)
_CONFIRMED_ONLY = re.compile(
    r"authoriz|sponsor|visa|citizen|nationality|clearance|certif|licen[cs]|"
    r"gender|race|ethnic|veteran|disabil|demographic|consent|agree|declar|attest",
    re.I,
)


@dataclass(frozen=True)
class FieldDecision:
    action: str  # fill | skip | block_job
    value: str | None
    source: str
    reason: str


def resolve_no_input(field: FieldSpec, supported: AnswerResult | None = None) -> FieldDecision:
    """Consume FormBrain evidence; never ask the user or fabricate a missing fact.

    A blocked required answer blocks only this job. The caller records it and
    continues with the next job. Punctuation is never used to fake completeness.
    """
    confirmed_only = bool(_CONFIRMED_ONLY.search(field.label))
    valid_source = not confirmed_only or (supported and supported.source == "user_confirmed")
    if (
        supported
        and supported.confident
        and valid_source
        and supported.value
        and re.search(r"\w", supported.value)
        and supported.value.strip().upper() != "UNKNOWN"
    ):
        value = supported.value.strip()
        if field.options:
            matches = [option for option in field.options if option.casefold() == value.casefold()]
            if matches:
                return FieldDecision("fill", matches[0], supported.source, "supported_answer")
        elif field.kind not in {"checkbox", "radio", "select"}:
            return FieldDecision("fill", value, supported.source, "supported_answer")
        # A mismatched option is not silently coerced to Yes or to the first item.
    if not field.required:
        return FieldDecision("skip", None, "policy", "optional_unknown")
    if _DEMOGRAPHIC.search(field.label):
        decline = next((option for option in field.options if _DECLINE.search(option)), None)
        if decline:
            return FieldDecision("fill", decline, "policy", "demographic_decline_option")
    if field.kind in {"text", "textarea"} and _SUPPLEMENTARY.fullmatch(field.label.strip()):
        return FieldDecision(
            "fill", "No additional information.", "policy", "neutral_supplementary"
        )
    return FieldDecision("block_job", None, "policy", "required_answer_not_supported")
