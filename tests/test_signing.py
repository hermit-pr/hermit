"""Tests for webhook signature validation."""

import hashlib
import hmac

import pytest

from hermit.signing import (
    WebhookValidationError,
    validate_github_signature,
    validate_gitlab_token,
)

SECRET = "0123456789abcdef"


def _sign(body: bytes) -> str:
    """Return the HMAC signature header for ``body``."""
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_github_signature_accepts_valid_body() -> None:
    """A body signed with the shared secret is accepted."""
    body = b'{"hello": "world"}'
    validate_github_signature(SECRET, body, _sign(body))


def test_github_signature_rejects_wrong_body() -> None:
    """A body whose signature does not match is rejected."""
    with pytest.raises(WebhookValidationError):
        validate_github_signature(SECRET, b'{"tampered": true}', _sign(b'{"ok": true}'))


def test_github_signature_requires_header() -> None:
    """A signature is required for every GitHub webhook."""
    with pytest.raises(WebhookValidationError):
        validate_github_signature(SECRET, b"{}", None)


def test_gitlab_token_accepts_valid_secret() -> None:
    """A token matching the shared secret is accepted."""
    validate_gitlab_token(SECRET, SECRET)


def test_gitlab_token_rejects_wrong_secret() -> None:
    """A token not matching the shared secret is rejected."""
    with pytest.raises(WebhookValidationError):
        validate_gitlab_token(SECRET, "not-the-secret")
