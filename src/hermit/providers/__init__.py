"""Provider selection for the configured Git hosting platform."""

import logging

from pydantic import SecretStr

from hermit.config import Settings
from hermit.providers.base import GitClient
from hermit.providers.github import GitHubClient
from hermit.providers.gitlab import GitLabClient

logger = logging.getLogger(__name__)


def build_git_client(settings: Settings) -> GitClient:
    """Return a client for the provider configured in ``settings``.

    Raises:
        ValueError: if the provider is unknown or its token is missing.
    """
    if settings.git_provider == "github":
        token = _resolve_rw_token(
            settings.github_token, settings.github_token_map, "HERMIT_GITHUB_TOKEN"
        )
        logger.info("built GitHub client for %s", settings.git_host_url)
        return GitHubClient(
            settings.git_host_url,
            token,
            org_tokens=settings.github_token_map,
        )
    if settings.git_provider == "gitlab":
        if settings.gitlab_token is None:
            raise ValueError(
                "HERMIT_GITLAB_TOKEN is required when provider is 'gitlab'"
            )
        logger.info("built GitLab client for %s", settings.git_host_url)
        return GitLabClient(
            settings.git_host_url, settings.gitlab_token.get_secret_value()
        )
    raise ValueError(f"unsupported provider: {settings.git_provider}")


def _resolve_rw_token(
    token: SecretStr | None,
    token_map: dict[str, str],
    env_name: str,
) -> str:
    """Return the default token, resolving from *token_map* if *token* is None.

    When only a token map is configured (no global fallback), the client still
    needs a default token string for construction.  The first mapped value is
    used — it is never actually sent because every request is routed through
    the per-org resolver.
    """
    if token is not None:
        return token.get_secret_value()
    if token_map:
        return next(iter(token_map.values()))
    raise ValueError(f"{env_name} or {env_name}_MAP is required")
