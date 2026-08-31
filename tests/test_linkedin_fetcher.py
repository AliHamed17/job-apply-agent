"""Exact LinkedIn URL fetches use the operator's isolated session."""

from pathlib import Path

from core.config import Settings


def test_linkedin_fetch_uses_persistent_profile_and_not_anonymous_browser(
    tmp_path, monkeypatch
) -> None:
    from jobs import fetcher

    profile_dir = tmp_path / "linkedin-profile"
    profile_dir.mkdir()
    settings = Settings(
        _env_file=None,
        linkedin_browser_profile_dir=str(profile_dir),
        polite_crawl_delay_seconds=0,
    )
    seen: dict[str, object] = {}

    async def fake_browser_fetch(url: str, *, profile_dir: str | None = None) -> str:
        seen["url"] = url
        seen["profile_dir"] = profile_dir
        return "<html><body>Machine Learning Engineer</body></html>"

    monkeypatch.setattr(fetcher, "get_settings", lambda: settings)
    monkeypatch.setattr(fetcher, "_fetch_browser", fake_browser_fetch)
    fetcher._page_cache.clear()

    result = fetcher.fetch_page("https://www.linkedin.com/jobs/view/123456")

    assert result.success is True
    assert seen == {
        "url": "https://www.linkedin.com/jobs/view/123456",
        "profile_dir": str(Path(profile_dir)),
    }


def test_linkedin_fetch_requires_an_existing_isolated_profile(tmp_path, monkeypatch) -> None:
    from jobs import fetcher

    settings = Settings(
        _env_file=None,
        linkedin_browser_profile_dir=str(tmp_path / "missing-profile"),
        polite_crawl_delay_seconds=0,
    )
    monkeypatch.setattr(fetcher, "get_settings", lambda: settings)
    fetcher._page_cache.clear()

    result = fetcher.fetch_page("https://www.linkedin.com/jobs/view/999")

    assert result.success is False
    assert result.blocked is True
    assert result.error == "LINKEDIN_SESSION_REQUIRED"


def test_linkedin_auth_shell_is_not_cached_as_a_success(tmp_path, monkeypatch) -> None:
    from jobs import fetcher

    profile_dir = tmp_path / "linkedin-profile"
    profile_dir.mkdir()
    settings = Settings(
        _env_file=None,
        linkedin_browser_profile_dir=str(profile_dir),
        polite_crawl_delay_seconds=0,
    )

    async def fake_browser_fetch(url: str, *, profile_dir: str | None = None) -> str:
        del url, profile_dir
        return "<html><title>Sign in | LinkedIn</title></html>"

    monkeypatch.setattr(fetcher, "get_settings", lambda: settings)
    monkeypatch.setattr(fetcher, "_fetch_browser", fake_browser_fetch)
    fetcher._page_cache.clear()

    result = fetcher.fetch_page("https://www.linkedin.com/jobs/view/555")

    assert result.success is False
    assert result.blocked is True
    assert result.error == "LINKEDIN_SESSION_REQUIRED"
    assert "https://www.linkedin.com/jobs/view/555" not in fetcher._page_cache
