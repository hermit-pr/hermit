"""Validation of incoming webhook authentication."""

import hashlib
import hmac


class WebhookValidationError(ValueError):
    """Raised when a webhook cannot be authenticated."""


def validate_github_signature(secret: str, body: bytes, header: str | None) -> None:
    """Verify the ``X-Hub-Signature-256`` header of a GitHub webhook.

    Raises:
        WebhookValidationError: if the header is missing or does not match.
    """
    if header is None:
        raise WebhookValidationError("missing X-Hub-Signature-256 header")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise WebhookValidationError("invalid GitHub webhook signature")


def validate_gitlab_token(secret: str, header: str | None) -> None:
    """Verify the ``X-Gitlab-Token`` header of a GitLab webhook.

    Raises:
        WebhookValidationError: if the header is missing or does not match.
    """
    if header is None:
        raise WebhookValidationError("missing X-Gitlab-Token header")
    if not hmac.compare_digest(header, secret):
        raise WebhookValidationError("invalid GitLab webhook token")
