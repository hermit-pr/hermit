"""GitHub REST API integration."""

import logging

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

    async def post_review(self, event: ChangeEvent, body: str) -> None:
        """Submit a general review comment on the pull request."""
        path = f"/repos/{event.repo}/pulls/{event.ref}/reviews"
        payload = {"body": body, "event": "COMMENT", "commit_id": event.head_sha}
        logger.info("posting review on %s %s/%s", self.endpoint, event.repo, event.ref)
        response = await self._request("POST", path, json=payload)
        response.raise_for_status()

    async def set_commit_status(
        self, event: ChangeEvent, state: str, description: str, context: str
    ) -> None:
        """Set a commit status (pending/success/failure/error) on the head commit."""
        path = f"/repos/{event.repo}/statuses/{event.head_sha}"
        payload = {
            "state": state,
            "description": description[:140],
            "context": context,
        }
        if event.url:
            payload["target_url"] = event.url
        logger.debug(
            "setting commit status %s on %s/%s", state, event.repo, event.head_sha[:8]
        )
        response = await self._request("POST", path, json=payload)
        response.raise_for_status()

    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Fetch the pull request to fill in the head/base refs."""
        path = f"/repos/{event.repo}/pulls/{event.ref}"
        logger.debug("resolving refs for %s/%s", event.repo, event.ref)
        response = await self._request("GET", path)
        response.raise_for_status()
        pull = response.json()
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        return ChangeEvent(
            provider="github",
            action=event.action,
            repo=event.repo,
            ref=event.ref,
            head_sha=head.get("sha") or "",
            head_ref=head.get("ref") or "",
            base_sha=base.get("sha") or "",
            base_ref=base.get("ref") or "",
            pr_title=pull.get("title") or "",
            pr_body=pull.get("body") or "",
            url=pull.get("html_url") or "",
            project_id=event.project_id,
        )

    async def check_membership(self, org: str, username: str) -> bool:
        """Return True when ``username`` is a member of ``org``.

        First tries the org membership endpoint (requires ``read:org`` scope).
        Falls back to the repo collaborator endpoint (requires only ``repo`` scope)
        to handle outside collaborators.
        """
        path = f"/orgs/{org}/members/{username}"
        response = await self._request("GET", path)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        if response.status_code in (302, 403):
            logger.warning(
                "org membership check returned %d for %s/%s; "
                "token may lack 'read:org' scope",
                response.status_code,
                org,
                username,
            )
            return False
        if response.status_code >= 400:
            logger.warning(
                "org membership check returned %d for %s/%s; assuming non-member",
                response.status_code,
                org,
                username,
            )
            return False
        response.raise_for_status()
        return True

    async def check_repo_collaborator(self, repo: str, username: str) -> bool:
        """Return True when ``username`` is a collaborator on ``repo``.

        Uses the repo collaborator endpoint which requires only ``repo`` scope.
        This works for both org members and outside collaborators.
        """
        path = f"/repos/{repo}/collaborators/{username}"
        response = await self._request("GET", path)
        if response.status_code == 204:
            return True
        if response.status_code == 404:
            return False
        if response.status_code in (302, 403):
            logger.warning(
                "repo collaborator check returned %d for %s/%s",
                response.status_code,
                repo,
                username,
            )
            return False
        response.raise_for_status()
        return False
