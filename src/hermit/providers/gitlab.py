"""GitLab REST API integration."""

import logging
from urllib.parse import quote

from hermit.models import ChangeEvent
from hermit.providers.base import GitClient

logger = logging.getLogger(__name__)


class GitLabClient(GitClient):
    """Client for the GitLab REST API.

    Uses a ``PRIVATE-TOKEN`` header with a token scoped to the projects the
    bot is allowed to review.
    """

    def headers(self) -> dict[str, str]:
        """Return the headers used to authenticate GitLab requests."""
        return {"PRIVATE-TOKEN": self._token}

    @staticmethod
    def _project(project: str) -> str:
        """URL-encode a ``namespace/project`` path for API calls."""
        return quote(project, safe="")

    async def post_review(self, event: ChangeEvent, body: str) -> None:
        """Add a note with the review on the merge request."""
        project = self._project(event.repo)
        path = f"/projects/{project}/merge_requests/{event.ref}/notes"
        logger.info("posting review on %s %s/%s", self.endpoint, event.repo, event.ref)
        response = await self._request("POST", path, json={"body": body})
        response.raise_for_status()

    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Fetch the merge request to fill in the head/base refs."""
        project = self._project(event.repo)
        path = f"/projects/{project}/merge_requests/{event.ref}"
        logger.debug("resolving refs for %s/%s", event.repo, event.ref)
        response = await self._request("GET", path)
        response.raise_for_status()
        merge_request = response.json()
        diff_refs = merge_request.get("diff_refs") or {}
        return ChangeEvent(
            provider="gitlab",
            action=event.action,
            repo=event.repo,
            ref=event.ref,
            head_sha=diff_refs.get("head_sha") or merge_request.get("sha", ""),
            head_ref=merge_request.get("source_branch", ""),
            base_sha=diff_refs.get("base_sha", ""),
            base_ref=merge_request.get("target_branch", ""),
            title=merge_request.get("title", ""),
            url=merge_request.get("web_url", ""),
            project_id=merge_request.get("iid"),
            source_repo=event.source_repo,
        )
