"""Sanitized real-DOM rehearsal for the evidence-backed Lever transport."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FieldType,
)
from submitters.lever_identity import parse_lever_posting_identity
from submitters.lever_playwright import (
    LeverFinalActionAmbiguousError,
    LeverNetworkGuard,
    PlaywrightLeverCandidateSession,
)
from submitters.lever_v1 import (
    LeverAdapterBlockedError,
    lever_v1_final_action_binding,
    lever_v1_form_fingerprint,
    observe_lever_v1_fields,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "lever_v1" / "application_basic.html"
POSTING = "11111111-2222-4333-8444-555555555555"
APPLY_URL = f"https://jobs.lever.co/sample-company/{POSTING}/apply"
CV_BYTES = b"%PDF-1.4\nchromium rehearsal fixture\n%%EOF\n"


def _resolver(_host, _port, _family, _socket_type):
    return [(2, 1, 6, "", ("8.8.8.8", 443))]


def _answer(field) -> object:
    if field.field_type is FieldType.FILE:
        return VERIFIED_ATTACHMENT_SENTINEL
    if field.field_type is FieldType.EMAIL:
        return "candidate@example.test"
    if field.field_type is FieldType.PHONE:
        return "+12025550100"
    if field.field_type is FieldType.URL:
        return "https://example.test/profile"
    if field.field_type is FieldType.DATE:
        return "2026-08-10"
    if field.field_type is FieldType.NUMBER:
        return "1"
    if field.field_type in {FieldType.SELECT, FieldType.RADIO}:
        return field.options[0].value
    if field.field_type is FieldType.MULTI_SELECT:
        return (field.options[0].value,)
    if field.field_type in {FieldType.CHECKBOX, FieldType.CONSENT, FieldType.ATTESTATION}:
        return True
    return "Fixture value"


@pytest.mark.asyncio
async def test_real_dom_rehearsal_never_submits_or_leaks_request() -> None:
    playwright = pytest.importorskip("playwright.async_api")
    try:
        browser = await playwright.async_playwright().start()
        browser_instance = await browser.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - environment-dependent browser install
        pytest.skip(f"Chromium unavailable for local rehearsal: {exc}")

    html = FIXTURE.read_text(encoding="utf-8")
    identity = parse_lever_posting_identity(APPLY_URL)
    cv_sha256 = hashlib.sha256(CV_BYTES).hexdigest()
    page = await browser_instance.new_page()
    requests: list[str] = []
    await page.add_init_script(
        """
        window.__rehearsalSubmitCalls = 0;
        HTMLFormElement.prototype.requestSubmit = function () {
            window.__rehearsalSubmitCalls += 1;
        };
        """
    )

    async def fulfill_fixture(route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route(
        APPLY_URL,
        fulfill_fixture,
    )
    page.on("request", lambda request: requests.append(request.method))

    try:
        await page.goto(APPLY_URL, wait_until="domcontentloaded")
        session = PlaywrightLeverCandidateSession()
        session._guard = LeverNetworkGuard(APPLY_URL, resolver=_resolver)
        session._identity = identity
        session._page = page

        snapshot = await session.snapshot()
        fields = observe_lever_v1_fields(snapshot.html, identity=identity)
        reviewed_blank_fields = {"org", "urls_Twitter_", "urls_Other_"}
        decisions = tuple(
            AnswerDecisionV1(
                field_id=field.field_id,
                disposition=(
                    AnswerDisposition.OPERATOR_CONFIRMED_BLANK
                    if field.field_id in reviewed_blank_fields
                    else AnswerDisposition.RESOLVED
                ),
                provenance=(
                    AnswerProvenance.OPERATOR_CONFIRMED
                    if field.field_id in reviewed_blank_fields
                    else (
                        AnswerProvenance.VERIFIED_ATTACHMENT
                        if field.field_type is FieldType.FILE
                        else AnswerProvenance.USER_CONFIRMED
                    )
                ),
                value=(None if field.field_id in reviewed_blank_fields else _answer(field)),
                evidence_refs=(
                    (f"operator_confirmation:{field.field_id}",)
                    if field.field_id in reviewed_blank_fields
                    else (f"fixture:{field.field_id}",)
                ),
            )
            for field in fields
        )

        await page.locator('input[name="resume"]').set_input_files(
            {"name": "rehearsal.pdf", "mimeType": "application/pdf", "buffer": CV_BYTES}
        )
        await session.ensure_resume_attachment(
            resume_bytes=CV_BYTES,
            cv_id="fixture-cv",
            expected_sha256=cv_sha256,
        )
        await session.fill(decisions)
        binding = lever_v1_final_action_binding(snapshot.html, identity=identity, fields=fields)
        fingerprint = lever_v1_form_fingerprint(identity, fields, binding)
        proof = await session.prepare_final_action(
            identity=identity,
            fields=fields,
            decisions=decisions,
            form_fingerprint=fingerprint,
            attached_cv_sha256=cv_sha256,
        )

        with pytest.raises(LeverFinalActionAmbiguousError):
            await session.click_final_action(proof)

        assert await page.evaluate("window.__rehearsalSubmitCalls") == 1
        assert requests == ["GET"]
        assert session._final_request_count == 0
    except LeverAdapterBlockedError as exc:
        pytest.fail(f"evidence-backed Lever DOM rehearsal blocked: {exc}")
    finally:
        await page.close()
        await browser_instance.close()
        await browser.stop()
