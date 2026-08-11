"""Tests for the application settings."""

import os

import pytest
from conftest import make_settings
from pydantic import ValidationError

from hermit.config import DEFAULT_REVIEW_RULES, Settings, SlaveSettings


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
    """The hardcoded default rules define the JSON verdict structure."""
    assert '"critical"' in DEFAULT_REVIEW_RULES
    assert '"medium"' in DEFAULT_REVIEW_RULES
    assert '"low"' in DEFAULT_REVIEW_RULES
    assert '"verdict"' in DEFAULT_REVIEW_RULES


def test_settings_review_rules_defaults_to_empty() -> None:
    """Without explicit rules the bot uses its hardcoded default in the prompt."""
    assert make_settings().review_rules == ""


def test_github_provider_requires_github_token() -> None:
    """Settings for the GitHub provider without a token are rejected."""
    with pytest.raises(ValidationError):
        make_settings(git_provider="github", github_token=None, git_read_token=None)


def test_github_provider_accepts_token_map() -> None:
    """GitHub provider can use a token map instead of a global token."""
    settings = make_settings(
        git_provider="github",
        github_token=None,
        github_token_map={"acme": "ghp_test123"},
        git_read_token=None,
        git_read_token_map={"acme": "ghp_read456"},
    )
    assert settings.github_token is None
    assert settings.github_token_map == {"acme": "ghp_test123"}
    assert settings.git_read_token is None
    assert settings.git_read_token_map == {"acme": "ghp_read456"}


def test_gitlab_provider_requires_gitlab_token() -> None:
    """Settings for the GitLab provider without a token are rejected."""
    with pytest.raises(ValidationError):
        make_settings(git_provider="gitlab", gitlab_token=None, git_read_token=None)


def test_max_concurrent_jobs_default() -> None:
    """The default concurrency cap is 20 jobs."""
    assert make_settings().max_concurrent_jobs == 20


def test_opencode_timeout_default() -> None:
    """The default opencode subprocess timeout is 900 seconds."""
    assert make_settings().opencode_timeout_seconds == 900


def test_token_map_rejects_malformed_json() -> None:
    """A malformed token map JSON string produces a clear ValueError."""
    with pytest.raises(ValueError, match="Invalid JSON"):
        make_settings(
            github_token_map='{"acme": "token"',  # missing closing brace
        )


def test_collect_org_tokens_from_env() -> None:
    """Per-org env vars are collected into token maps."""
    monkeypatch_env = {
        "HERMIT_GITHUB_RW_ORG_0_NAME": "acme",
        "HERMIT_GITHUB_RW_ORG_0_TOKEN": "ghp_rw_token",
        "HERMIT_GITHUB_RW_ORG_1_NAME": "other",
        "HERMIT_GITHUB_RW_ORG_1_TOKEN": "ghp_rw_other",
        "HERMIT_GIT_READ_ORG_0_NAME": "acme",
        "HERMIT_GIT_READ_ORG_0_TOKEN": "ghp_read_token",
    }
    saved = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        settings = make_settings(github_token=None, git_read_token=None)
    finally:
        for k in monkeypatch_env:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]
    assert settings.github_token_map == {
        "acme": "ghp_rw_token",
        "other": "ghp_rw_other",
    }
    assert settings.git_read_token_map == {"acme": "ghp_read_token"}


def test_collect_org_tokens_merges_with_existing_map() -> None:
    """Env var tokens merge with any existing token_map value."""
    monkeypatch_env = {
        "HERMIT_GITHUB_RW_ORG_0_NAME": "acme",
        "HERMIT_GITHUB_RW_ORG_0_TOKEN": "ghp_env_token",
    }
    saved = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        settings = make_settings(
            github_token=None,
            github_token_map={"other": "ghp_other"},
            git_read_token=None,
            git_read_token_map={"acme": "ghp_read"},
        )
    finally:
        for k in monkeypatch_env:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]
    assert settings.github_token_map == {"acme": "ghp_env_token", "other": "ghp_other"}


def test_empty_secretstr_coerced_to_none() -> None:
    """An empty SecretStr token is treated as None (absent secretKeyRef)."""
    settings = Settings(
        webhook_secret="0123456789abcdef",
        git_read_token=None,
        git_read_token_map={"acme": "ghp_read"},
        github_token="",  # empty string should be coerced to None
        github_token_map={"acme": "ghp_test"},
        vllm_endpoint="http://vllm.example:8000/v1",
        model="test-model",
        master_url="http://hermit:8080",
        pod_image="hermit:test",
    )
    assert settings.github_token is None


def test_collect_org_tokens_ignores_unmatched_env() -> None:
    """Env vars without matching _TOKEN pair are not collected."""
    monkeypatch_env = {
        "HERMIT_GITHUB_RW_ORG_0_NAME": "lonely",
    }
    saved = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        settings = make_settings(
            github_token=None,
            git_read_token=None,
            github_token_map={"acme": "ghp_rw"},
            git_read_token_map={"acme": "ghp_read"},
        )
    finally:
        for k in monkeypatch_env:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]
    assert settings.github_token_map == {"acme": "ghp_rw"}
    assert settings.git_read_token_map == {"acme": "ghp_read"}


def test_github_provider_validates_after_org_token_collection() -> None:
    """Validation passes when only per-org env vars supply GitHub tokens."""
    monkeypatch_env = {
        "HERMIT_GITHUB_RW_ORG_0_NAME": "acme",
        "HERMIT_GITHUB_RW_ORG_0_TOKEN": "ghp_rw_token",
        "HERMIT_GIT_READ_ORG_0_NAME": "acme",
        "HERMIT_GIT_READ_ORG_0_TOKEN": "ghp_read_token",
    }
    saved = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        settings = make_settings(
            git_provider="github",
            github_token=None,
            git_read_token=None,
        )
    finally:
        for k in monkeypatch_env:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]
    assert settings.github_token is None
    assert settings.git_read_token is None
    assert settings.github_token_map == {"acme": "ghp_rw_token"}
    assert settings.git_read_token_map == {"acme": "ghp_read_token"}
