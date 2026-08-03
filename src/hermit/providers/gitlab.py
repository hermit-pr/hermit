"""GitLab REST API integration."""

from typing import Any
from urllib.parse import quote

from hermit.models import ChangeEvent
from hermit.providers.base import GitClient


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

    @staticmethod
    def _format_diff(diff: dict[str, Any]) -> str:
        """Render a single diff entry of the merge request."""
        old_path = diff.get("old_path", "")
        new_path = diff.get("new_path", "")
        path = old_path if old_path == new_path else f"{old_path} -> {new_path}"
        return f"### {path}\n{diff.get('diff', '')}"

    async def fetch_diff(self, event: ChangeEvent) -> str:
        """Fetch and render the diffs of a merge request."""
        project = self._project(event.repo)
        path = f"/projects/{project}/merge_requests/{event.ref}/diffs"
        response = await self._http.get(path)
        response.raise_for_status()
        diffs = response.json()
        return "\n\n".join(self._format_diff(diff) for diff in diffs)

    async def post_review(self, event: ChangeEvent, body: str) -> None:
        """Add a note with the review on the merge request."""
        project = self._project(event.repo)
        path = f"/projects/{project}/merge_requests/{event.ref}/notes"
        response = await self._http.post(path, json={"body": body})
        response.raise_for_status()

    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Fetch the merge request to fill in the head/base refs."""
        project = self._project(event.repo)
        path = f"/projects/{project}/merge_requests/{event.ref}"
        response = await self._http.get(path)
        response.raise_for_status()
        merge_request = response.json()
        diff_refs = merge_request.get("diff_refs") or {}
        return ChangeEvent(
            provider="gitlab",
            action=event.action,
            repo=event.repo,
            ref=event.ref,
            head_sha=merge_request.get("sha", ""),
            head_ref=merge_request.get("source_branch", ""),
            base_sha=diff_refs.get("base_sha", ""),
            base_ref=merge_request.get("target_branch", ""),
            title=merge_request.get("title", ""),
            url=merge_request.get("web_url", ""),
            project_id=merge_request.get("iid"),
        )
