"""Tests for the application settings."""

import pytest
from conftest import make_settings
from pydantic import ValidationError


def test_settings_accepts_minimal_keyword_arguments() -> None:
    """Settings can be built from the required keyword arguments."""
    settings = make_settings()
    assert settings.git_provider == "github"
    assert settings.port == 8080
    assert settings.opencode_args == ["run"]
    assert settings.report_timeout_seconds == 1800


def test_settings_requires_webhook_secret() -> None:
    """Settings without a webhook secret are rejected."""
    with pytest.raises(ValidationError):
        make_settings(webhook_secret=None)


def test_settings_splits_opencode_args() -> None:
    """A whitespace separated argument string is split into a list."""
    settings = make_settings(opencode_args="run --format json")
    assert settings.opencode_args == ["run", "--format", "json"]


def test_settings_default_review_rules_define_sections() -> None:
    """The default rules ask for the fixed review sections."""
    rules = make_settings().review_rules
    assert "Critical changes that need to be fixed" in rules
    assert "Medium issues" in rules
    assert "Low issues" in rules
    assert "General feedback" in rules
