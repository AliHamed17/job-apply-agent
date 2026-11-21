from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import submitters.linkedin_v2 as linkedin
from submitters.linkedin_v2 import LinkedInV2Submitter, StepPlan


@pytest.mark.asyncio
async def test_unverified_field_stops_before_submit(monkeypatch):
    locator = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    locator.first = locator
    page = SimpleNamespace(
        url="https://www.linkedin.com/jobs/view/123",
        goto=AsyncMock(),
        wait_for_timeout=AsyncMock(),
        content=AsyncMock(return_value=""),
        locator=MagicMock(return_value=locator),
    )
    runner = LinkedInV2Submitter()
    monkeypatch.setattr(runner, "detect_captcha", lambda _: False)
    monkeypatch.setattr(runner, "_discard", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_fill_step", AsyncMock(return_value=["Email"]))
    monkeypatch.setattr(linkedin, "parse_fields", lambda _: [])
    monkeypatch.setattr(
        linkedin, "resolve_step", AsyncMock(return_value=StepPlan({"Email": "a"}, []))
    )
    result = await runner._apply(
        page, page.url, None, None, None, SimpleNamespace(dry_run=False), None
    )
    assert result.status == "draft_only"
    assert result.error == "FIELD_FILL_UNVERIFIED"
    assert page.locator.call_count == 1
    assert locator.click.await_count == 1  # Easy Apply only, never Submit.


@pytest.mark.asyncio
async def test_text_field_requires_exact_readback():
    label = SimpleNamespace(
        count=AsyncMock(return_value=1), get_attribute=AsyncMock(return_value="email")
    )
    label.first = label
    target = SimpleNamespace(
        count=AsyncMock(return_value=1),
        evaluate=AsyncMock(return_value="input"),
        is_visible=AsyncMock(return_value=True),
        is_editable=AsyncMock(return_value=True),
        fill=AsyncMock(),
        input_value=AsyncMock(return_value="wrong@example.test"),
    )

    def locator(selector):
        return target if selector == "#email" else label

    page = SimpleNamespace(locator=locator)
    assert not await LinkedInV2Submitter()._fill_by_label(page, "Email", "right@example.test")
