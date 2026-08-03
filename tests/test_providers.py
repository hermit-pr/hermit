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
        title="Add endpoint",
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
        title="Add endpoint",
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
                "diff_refs": {"base_sha": "bbb222"},
            },
        )

    client = GitLabClient("https://git.example", "token123", http=_http(handler))
    try:
        event = await client.resolve_refs(_gitlab_event())
    finally:
        await client.aclose()
    assert event.head_ref == "feature/x"
    assert event.head_sha == "aaa111"
    assert event.base_ref == "main"
    assert event.base_sha == "bbb222"
