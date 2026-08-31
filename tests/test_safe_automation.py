"""Regression coverage for the local discovery-only runner gate."""

from profile.models import UserProfile

import pytest

from core.config import Settings
from scripts.run_safe_automation import validate_discovery_profile


def _profile(*, identity: bool) -> UserProfile:
    profile = UserProfile()
    profile.preferences.roles = ["Machine Learning Engineer"]
    profile.preferences.locations = ["Israel", "Worldwide Remote"]
    profile.preferences.remote_ok = True
    if identity:
        profile.personal.name = "Example Candidate"
        profile.personal.email = "candidate@example.test"
        profile.personal.location = "Tel Aviv, Israel"
    return profile


def test_discovery_runner_does_not_require_private_identity() -> None:
    """Discovery may run before onboarding; private identity gates later stages."""

    validate_discovery_profile(_profile(identity=False))


def test_discovery_runner_still_rejects_missing_search_scope() -> None:
    profile = UserProfile()
    profile.personal.name = "Example Candidate"
    profile.personal.email = "candidate@example.test"
    profile.personal.location = "Tel Aviv, Israel"
    with pytest.raises(RuntimeError, match="PROFILE_TARGET_ROLES_MISSING"):
        validate_discovery_profile(profile)


def test_safe_mode_guard_remains_strict() -> None:
    from scripts.run_safe_automation import validate_safe_mode

    with pytest.raises(RuntimeError, match="DRY_RUN"):
        validate_safe_mode(Settings(_env_file=None, dry_run=False, draft_only=True))
