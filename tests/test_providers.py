"""Tests for the Git hosting provider clients."""

import json
from typing import Callable

import httpx
import pytest

from hermit.models import ChangeEvent
from hermit.providers.github import GitHubClient
from hermit.providers.gitlab import GitLabClient


def _github_event() -> ChangeEvent:
    """Return a sample GitHub pull request event."""
    return ChangeEvent(
        provider="github",
        action="opened",
        repo="acme/app",
        ref="7",
        head_sha="abc123",
        head_ref="feature/x",
        base_sha="def456",
        base_ref="main",
        pr_title="Add endpoint",
        url="https://github.example/acme/app/pull/7",
    )


def _gitlab_event() -> ChangeEvent:
    """Return a sample GitLab merge request event."""
    return ChangeEvent(
        provider="gitlab",
        action="open",
        repo="acme/app",
        ref="9",
        head_sha="abc123",
        head_ref="feature/x",
        base_ref="main",
        pr_title="Add endpoint",
        url="https://gitlab.example/acme/app/-/merge_requests/9",
    )


def _http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Return an AsyncClient backed by the given mock handler."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://git.example"
    )


def test_github_headers_use_bearer_token() -> None:
    """GitHub authentication uses a bearer token."""
    client = GitHubClient("https://git.example", "token123")
    assert client.headers()["Authorization"] == "Bearer token123"


def test_gitlab_headers_use_private_token() -> None:
    """GitLab authentication uses a private token."""
    client = GitLabClient("https://git.example", "token123")
    assert client.headers()["PRIVATE-TOKEN"] == "token123"


@pytest.mark.asyncio
async def test_github_post_review_posts_comment() -> None:
    """GitHub reviews are submitted to the pull request reviews endpoint."""
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        await client.post_review(_github_event(), "Looks good")
    finally:
        await client.aclose()
    assert posted == {"body": "Looks good", "event": "COMMENT", "commit_id": "abc123"}


@pytest.mark.asyncio
async def test_github_resolve_refs_fills_head_and_base() -> None:
    """GitHub refs are resolved from the pull request endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/app/pulls/7"
        return httpx.Response(
            200,
            json={
                "title": "Add endpoint",
                "html_url": "https://github.example/acme/app/pull/7",
                "head": {"ref": "feature/x", "sha": "aaa111"},
                "base": {"ref": "main", "sha": "bbb222"},
            },
        )

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        event = await client.resolve_refs(_github_event())
    finally:
        await client.aclose()
    assert event.head_ref == "feature/x"
    assert event.head_sha == "aaa111"
    assert event.base_ref == "main"
    assert event.base_sha == "bbb222"


@pytest.mark.asyncio
async def test_gitlab_post_review_adds_note() -> None:
    """GitLab reviews are added to the merge request notes endpoint."""
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={})

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        await client.post_review(_gitlab_event(), "Looks good")
    finally:
        await client.aclose()
    assert posted == {"body": "Looks good"}


@pytest.mark.asyncio
async def test_gitlab_resolve_refs_fills_head_and_base() -> None:
    """GitLab refs are resolved from the merge request endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/projects/acme/app/merge_requests/9"
        return httpx.Response(
            200,
            json={
                "iid": 9,
                "title": "Add endpoint",
                "web_url": "https://gitlab.example/acme/app/-/merge_requests/9",
                "sha": "aaa111",
                "source_branch": "feature/x",
                "target_branch": "main",
                "diff_refs": {"base_sha": "bbb222", "head_sha": "ccc333"},
            },
        )

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        event = await client.resolve_refs(_gitlab_event())
    finally:
        await client.aclose()
    assert event.head_ref == "feature/x"
    assert event.head_sha == "ccc333"
    assert event.base_ref == "main"
    assert event.base_sha == "bbb222"


@pytest.mark.asyncio
async def test_gitlab_resolve_refs_head_sha_falls_back_to_merge_result() -> None:
    """Without diff_refs.head_sha the merge result sha is used."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sha": "aaa111",
                "source_branch": "feature/x",
                "target_branch": "main",
                "diff_refs": {"base_sha": "bbb222"},
            },
        )

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        event = await client.resolve_refs(_gitlab_event())
    finally:
        await client.aclose()
    assert event.head_sha == "aaa111"


@pytest.mark.asyncio
async def test_github_client_retries_transient_errors() -> None:
    """A 5xx is retried before the failure is surfaced."""
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        event = await client.resolve_refs(_github_event())
    finally:
        await client.aclose()
    assert calls["count"] == 3
    assert event.head_ref == ""


@pytest.mark.asyncio
async def test_github_client_does_not_retry_4xx() -> None:
    """A 4xx response is returned without retries."""
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(403, json={"message": "forbidden"})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.resolve_refs(_github_event())
    finally:
        await client.aclose()
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_github_check_membership_accepts_member() -> None:
    """A 204 on the membership endpoint means the user is a member."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/orgs/acme/members/alice"
        return httpx.Response(204, json={})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        member = await client.check_membership("acme", "alice")
    finally:
        await client.aclose()
    assert member is True


@pytest.mark.asyncio
async def test_github_check_membership_rejects_non_member() -> None:
    """A 404 on the membership endpoint means the user is not a member."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        member = await client.check_membership("acme", "mallory")
    finally:
        await client.aclose()
    assert member is False


@pytest.mark.asyncio
async def test_github_check_membership_handles_redirect() -> None:
    """A 302 redirect (e.g. to login) is treated as not a member."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://git.example/login"})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        member = await client.check_membership("acme", "mallory")
    finally:
        await client.aclose()
    assert member is False


@pytest.mark.asyncio
async def test_github_set_commit_status() -> None:
    """GitHub commit statuses are posted to the statuses endpoint."""
    event = ChangeEvent(
        provider="github",
        action="opened",
        repo="o/r",
        ref="1",
        head_sha="abc123",
        url="https://example.com/pr/1",
    )
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/o/r/statuses/abc123"
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={})

    client = GitHubClient("https://git.example", "token123", http=_http(handler))
    try:
        await client.set_commit_status(
            event, "pending", "H.E.R.M.I.T is reviewing...", "hermit/review"
        )
    finally:
        await client.aclose()
    assert posted == {
        "state": "pending",
        "description": "H.E.R.M.I.T is reviewing...",
        "context": "hermit/review",
        "target_url": "https://example.com/pr/1",
    }


@pytest.mark.asyncio
async def test_gitlab_set_commit_status() -> None:
    """GitLab commit statuses are posted to the statuses endpoint."""
    event = ChangeEvent(
        provider="gitlab",
        action="open",
        repo="o/r",
        ref="1",
        head_sha="abc123",
        project_id=42,
        head_ref="feature",
    )
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/projects/42/statuses/abc123"
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={})

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        await client.set_commit_status(
            event, "success", "Review completed.", "hermit/review"
        )
    finally:
        await client.aclose()
    assert posted == {
        "state": "success",
        "description": "Review completed.",
        "name": "hermit/review",
        "ref": "feature",
    }


@pytest.mark.asyncio
async def test_gitlab_check_membership_true() -> None:
    """A 200 on the group membership endpoint means the user is a member."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/groups/acme/members/alice"
        return httpx.Response(200, json={"id": 1, "username": "alice"})

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        member = await client.check_membership("acme", "alice")
    finally:
        await client.aclose()
    assert member is True


@pytest.mark.asyncio
async def test_gitlab_check_membership_not_found() -> None:
    """A 404 on the group membership endpoint means the user is not a member."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        member = await client.check_membership("acme", "mallory")
    finally:
        await client.aclose()
    assert member is False
