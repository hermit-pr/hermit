"""Tests for the application settings."""

import pytest
from conftest import make_settings
from pydantic import ValidationError

from hermit.config import DEFAULT_REVIEW_RULES, SlaveSettings


def test_settings_accepts_minimal_keyword_arguments() -> None:
    """Settings can be built from the required keyword arguments."""
    settings = make_settings()
    assert settings.git_provider == "github"
    assert settings.port == 8080
    assert settings.opencode_args == ["run", "--auto", "--format", "json"]
    assert settings.report_timeout_seconds == 1800


def test_slave_settings_accepts_ref_field() -> None:
    """SlaveSettings carries the ref used for GitHub pull ref fetches."""
    settings = SlaveSettings(
        job_id="job1",
        git_read_token="read-token",
        repo="acme/app",
        ref="42",
        vllm_endpoint="http://vllm.example:8000/v1",
        model="test-model",
        master_url="http://hermit:8080",
        report_secret="s3cr3t",
    )
    assert settings.ref == "42"


def test_slave_settings_ref_defaults_to_empty() -> None:
    """Without a ref the field defaults to an empty string."""
    settings = SlaveSettings(
        job_id="job1",
        git_read_token="read-token",
        repo="acme/app",
        vllm_endpoint="http://vllm.example:8000/v1",
        model="test-model",
        master_url="http://hermit:8080",
        report_secret="s3cr3t",
    )
    assert settings.ref == ""


def test_settings_requires_webhook_secret() -> None:
    """Settings without a webhook secret are rejected."""
    with pytest.raises(ValidationError):
        make_settings(webhook_secret=None)


def test_settings_splits_opencode_args() -> None:
    """A whitespace separated argument string is split into a list."""
    settings = make_settings(opencode_args="run --format json")
    assert settings.opencode_args == ["run", "--format", "json"]


def test_settings_default_review_rules_define_sections() -> None:
    """The hardcoded default rules ask for the fixed review sections."""
    assert "Critical changes that need to be fixed" in DEFAULT_REVIEW_RULES
    assert "Medium issues" in DEFAULT_REVIEW_RULES
    assert "Low issues" in DEFAULT_REVIEW_RULES
    assert "General feedback" in DEFAULT_REVIEW_RULES


def test_settings_review_rules_defaults_to_empty() -> None:
    """Without explicit rules the bot uses its hardcoded default in the prompt."""
    assert make_settings().review_rules == ""


def test_github_provider_requires_github_token() -> None:
    """Settings for the GitHub provider without a token are rejected."""
    with pytest.raises(ValidationError):
        make_settings(git_provider="github", github_token=None)


def test_gitlab_provider_requires_gitlab_token() -> None:
    """Settings for the GitLab provider without a token are rejected."""
    with pytest.raises(ValidationError):
        make_settings(git_provider="gitlab", gitlab_token=None)


def test_max_concurrent_jobs_default() -> None:
    """The default concurrency cap is 20 jobs."""
    assert make_settings().max_concurrent_jobs == 20


def test_opencode_timeout_default() -> None:
    """The default opencode subprocess timeout is 900 seconds."""
    assert make_settings().opencode_timeout_seconds == 900
