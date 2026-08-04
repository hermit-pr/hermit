"""Provider selection for the configured Git hosting platform."""

import logging

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
        if settings.github_token is None:
            raise ValueError(
                "HERMIT_GITHUB_TOKEN is required when provider is 'github'"
            )
        logger.info("built GitHub client for %s", settings.git_host_url)
        return GitHubClient(
            settings.git_host_url, settings.github_token.get_secret_value()
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
