"""GitHub REST API integration."""

import logging
from typing import Any

from hermit.models import ChangeEvent
from hermit.providers.base import GitClient

logger = logging.getLogger(__name__)


class GitHubClient(GitClient):
    """Client for the GitHub REST API.

    Uses ``Authorization: Bearer`` with a token scoped to the repositories
    the bot is allowed to review.
    """

    def headers(self) -> dict[str, str]:
        """Return the headers used to authenticate GitHub requests."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    @staticmethod
    def _format_file(file: dict[str, Any]) -> str:
        """Render a single file entry of the pull request diff."""
        filename = file.get("filename", "unknown")
        patch = file.get("patch", "")
        return f"### {filename}\n{patch}"

    async def fetch_diff(self, event: ChangeEvent) -> str:
        """Fetch and render the files changed by a pull request."""
        path = f"/repos/{event.repo}/pulls/{event.ref}/files"
        logger.debug("fetching diff for %s/%s", event.repo, event.ref)
        response = await self._http.get(path)
        response.raise_for_status()
        files = response.json()
        return "\n\n".join(self._format_file(file) for file in files)

    async def post_review(self, event: ChangeEvent, body: str) -> None:
        """Submit a general review comment on the pull request."""
        path = f"/repos/{event.repo}/pulls/{event.ref}/reviews"
        payload = {"body": body, "event": "COMMENT", "commit_id": event.head_sha}
        logger.info("posting review on %s %s/%s", self.endpoint, event.repo, event.ref)
        response = await self._http.post(path, json=payload)
        response.raise_for_status()

    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Fetch the pull request to fill in the head/base refs."""
        path = f"/repos/{event.repo}/pulls/{event.ref}"
        logger.debug("resolving refs for %s/%s", event.repo, event.ref)
        response = await self._http.get(path)
        response.raise_for_status()
        pull = response.json()
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        return ChangeEvent(
            provider="github",
            action=event.action,
            repo=event.repo,
            ref=event.ref,
            head_sha=head.get("sha", ""),
            head_ref=head.get("ref", ""),
            base_sha=base.get("sha", ""),
            base_ref=base.get("ref", ""),
            title=pull.get("title", ""),
            url=pull.get("html_url", ""),
            project_id=event.project_id,
        )
