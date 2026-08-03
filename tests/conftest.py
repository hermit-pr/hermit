"""Shared fixtures and helpers for the test suite."""

from typing import Any

from hermit.config import Settings

SECRET = "0123456789abcdef"


def make_settings(**overrides: Any) -> Settings:
    """Build master Settings from defaults plus overrides."""
    defaults: dict[str, Any] = {
        "webhook_secret": SECRET,
        "git_read_token": "read-token",
        "vllm_endpoint": "http://vllm.example:8000/v1",
        "model": "test-model",
        "master_url": "http://hermit:8080",
        "pod_image": "hermit:test",
    }
    defaults.update(overrides)
    return Settings(**defaults)
