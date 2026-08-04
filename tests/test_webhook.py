"""Tests for the master webhook server and review lifecycle."""

import asyncio
import hashlib
import hmac
import json

import httpx
from conftest import SECRET, make_settings

from hermit import __version__
from hermit.config import Settings
from hermit.jobs import JobStore
from hermit.k8s import FakePodSpawner
from hermit.models import ChangeEvent
from hermit.providers.base import GitClient
from hermit.server import create_app, parse_github, parse_gitlab


class FakeClient(GitClient):
    """In-memory Git client used to capture posted reviews."""

    def __init__(self) -> None:
        super().__init__(endpoint="https://ignored", token="fake")
        self.posted: list[str] = []
        self.statuses: list[str] = []
        self.resolved: dict[str, str] = {}

    def headers(self) -> dict[str, str]:
        """Return the auth header for the fake client."""
        return {"Authorization": "Bearer test"}

    async def post_review(self, _event: ChangeEvent, body: str) -> None:
        """Record a posted review body."""
        self.posted.append(body)

    async def set_commit_status(
        self, _event: ChangeEvent, state: str, description: str, context: str
    ) -> None:
        """Record a commit status update."""
        self.statuses.append(state)

    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Fill refs from the recorded override map."""
        return event.model_copy(update=self.resolved)

    async def aclose(self) -> None:
        """Close the fake client."""


class NoMembershipClient(FakeClient):
    """Fake client that rejects every membership check."""

    async def check_membership(self, _org: str, _username: str) -> bool:
        """Reject every commenter."""
        return False

    async def check_repo_collaborator(self, _repo: str, _username: str) -> bool:
        """Reject every commenter."""
        return False


def _settings(**overrides) -> Settings:
    """Build settings with a small report timeout for tests."""
    values = {"report_timeout_seconds": 1, "pod_spawner": "fake"}
    values.update(overrides)
    return make_settings(**values)


def _github_signature(body: bytes) -> str:
    """Return the HMAC signature header for ``body``."""
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _client(app) -> httpx.AsyncClient:
    """Build an ASGI client for ``app``."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _github_pr_payload() -> dict:
    """Return a GitHub pull request webhook payload."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "title": "Add endpoint",
            "body": "Adds the endpoint.",
            "html_url": "https://github.example/acme/app/pull/7",
            "head": {"ref": "feature/x", "sha": "abc123"},
            "base": {"ref": "main", "sha": "def456"},
        },
        "repository": {"full_name": "acme/app"},
    }


def _gitlab_payload() -> dict:
    """Return a GitLab merge request webhook payload."""
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "open",
            "iid": 9,
            "title": "Add endpoint",
            "description": "Adds the endpoint.",
            "source_branch": "feature/x",
            "target_branch": "main",
            "last_commit": {"id": "abc123"},
            "url": "https://gitlab.example/acme/app/-/merge_requests/9",
        },
        "project": {"path_with_namespace": "acme/app"},
    }


def _github_event() -> ChangeEvent:
    """Build a minimal GitHub change event."""
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
    )


async def test_healthz_returns_ok() -> None:
    """The health endpoint reports the bot as alive and versioned."""
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), JobStore())
    async with _client(app) as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_github_webhook_rejects_bad_signature() -> None:
    """GitHub webhooks with an invalid signature are rejected."""
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), JobStore())
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            json={"action": "opened"},
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )
    assert response.status_code == 401


async def test_github_webhook_publishes_review_via_pod() -> None:
    """A valid GitHub webhook spawns a pod and its report is published."""
    client = FakeClient()
    spawner = FakePodSpawner()
    store = JobStore()
    app = create_app(_settings(), client, spawner, store)
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=json.dumps(_github_pr_payload()).encode(),
            headers={
                "X-Hub-Signature-256": _github_signature(
                    json.dumps(_github_pr_payload()).encode()
                )
            },
        )
        assert response.status_code == 202
        job = store.all()[0]
        assert any(spawned[0].id == job.id for spawned in spawner.spawned)
        report = await http.post(
            f"/internal/report/{job.id}",
            json={"body": "Review: this looks good."},
            headers={"X-Hermit-Report-Secret": job.report_secret},
        )
        assert report.status_code == 200
    assert client.posted == ["Review: this looks good."]


async def test_github_webhook_ignores_other_events() -> None:
    """Webhooks that are not pull request events are ignored."""
    store = JobStore()
    spawner = FakePodSpawner()
    app = create_app(_settings(), FakeClient(), spawner, store)
    payload = {"ref": "refs/heads/main", "repository": {"full_name": "acme/app"}}
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={
                "X-Hub-Signature-256": _github_signature(json.dumps(payload).encode())
            },
        )
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert not store.all()
    assert not spawner.spawned


async def test_gitlab_webhook_publishes_review_via_pod() -> None:
    """A valid GitLab webhook spawns a pod and its report is published."""
    client = FakeClient()
    spawner = FakePodSpawner()
    store = JobStore()
    app = create_app(_settings(), client, spawner, store)
    async with _client(app) as http:
        response = await http.post(
            "/webhook/gitlab",
            json=_gitlab_payload(),
            headers={"X-Gitlab-Token": SECRET},
        )
        assert response.status_code == 202
        job = store.all()[0]
        report = await http.post(
            f"/internal/report/{job.id}",
            json={"body": "Review: ok."},
            headers={"X-Hermit-Report-Secret": job.report_secret},
        )
        assert report.status_code == 200
    assert client.posted == ["Review: ok."]


async def test_github_comment_trigger_spawns_pod() -> None:
    """An @hermit comment on a pull request triggers a pod."""
    client = FakeClient()
    client.resolved = {
        "head_sha": "abc123",
        "head_ref": "feature/x",
        "base_sha": "def456",
        "base_ref": "main",
    }
    store = JobStore()
    app = create_app(_settings(), client, FakePodSpawner(), store)
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "title": "Add endpoint",
            "pull_request": {},
            "html_url": "https://github.example/acme/app/pull/7",
        },
        "comment": {
            "body": "please @hermit review this",
            "user": {"login": "alice"},
        },
        "repository": {"full_name": "acme/app"},
    }
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={
                "X-Hub-Signature-256": _github_signature(json.dumps(payload).encode())
            },
        )
    assert response.status_code == 202
    assert len(store.all()) == 1
    job = store.all()[0]
    assert job.event.head_sha == "abc123"
    assert job.event.base_ref == "main"


async def test_github_comment_without_mention_is_ignored() -> None:
    """A PR comment without the bot mention is ignored."""
    store = JobStore()
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), store)
    payload = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {}},
        "comment": {"body": "just talking"},
        "repository": {"full_name": "acme/app"},
    }
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={
                "X-Hub-Signature-256": _github_signature(json.dumps(payload).encode())
            },
        )
    assert response.status_code == 202
    assert not store.all()


async def test_report_rejects_invalid_secret() -> None:
    """A report with the wrong secret is rejected."""
    store = JobStore()
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), store)
    job = await store.create(_github_event())
    async with _client(app) as http:
        response = await http.post(
            f"/internal/report/{job.id}",
            json={"body": "hi"},
            headers={"X-Hermit-Report-Secret": "wrong"},
        )
    assert response.status_code == 401


async def test_report_returns_404_for_unknown_job() -> None:
    """A report for an unknown job returns 404."""
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), JobStore())
    async with _client(app) as http:
        response = await http.post(
            "/internal/report/unknown",
            json={"body": "hi"},
            headers={"X-Hermit-Report-Secret": "whatever"},
        )
    assert response.status_code == 404


async def test_duplicate_report_is_ignored() -> None:
    """A repeated report does not post the review twice."""
    client = FakeClient()
    spawner = FakePodSpawner()
    store = JobStore()
    app = create_app(_settings(), client, spawner, store)
    job = await store.create(_github_event())
    await spawner.spawn(job, {})
    async with _client(app) as http:
        first = await http.post(
            f"/internal/report/{job.id}",
            json={"body": "rev"},
            headers={"X-Hermit-Report-Secret": job.report_secret},
        )
        second = await http.post(
            f"/internal/report/{job.id}",
            json={"body": "rev again"},
            headers={"X-Hermit-Report-Secret": job.report_secret},
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert client.posted == ["rev"]


async def test_report_survives_master_restart() -> None:
    """A report can be served by k8s state after the in-memory store is lost."""
    client = FakeClient()
    spawner = FakePodSpawner()
    store = JobStore()
    app = create_app(_settings(), client, spawner, store)
    job = await store.create(_github_event())
    await spawner.spawn(job, {})
    await store.remove(job.id)
    async with _client(app) as http:
        response = await http.post(
            f"/internal/report/{job.id}",
            json={"body": "rev"},
            headers={"X-Hermit-Report-Secret": job.report_secret},
        )
    assert response.status_code == 200
    assert client.posted == ["rev"]


async def test_job_ids_are_deterministic_per_event() -> None:
    """The same event maps to the same job id across stores; secrets differ."""
    event = _github_event()
    first = await JobStore(signing_key="key").create(event)
    second = await JobStore(signing_key="key").create(event)
    assert first.id == second.id
    assert first.report_secret != second.report_secret
    other = event.model_copy(update={"head_sha": "different"})
    third = await JobStore(signing_key="key").create(other)
    assert third.id != first.id


def test_parse_gitlab_captures_fork_source_repo() -> None:
    """A cross-project (fork) MR records the source project path."""
    payload = _gitlab_payload()
    payload["object_attributes"]["source"] = {"path_with_namespace": "fork/app"}
    event = parse_gitlab(payload)
    assert event is not None
    assert event.source_repo == "fork/app"


def test_parse_gitlab_same_project_has_no_source_repo() -> None:
    """A same-project MR leaves source_repo empty."""
    event = parse_gitlab(_gitlab_payload())
    assert event is not None
    assert event.source_repo == ""
    assert event.pr_title == "Add endpoint"
    assert event.pr_body == "Adds the endpoint."


def test_parse_github_builds_pr_change_event() -> None:
    """GitHub pull request payloads are normalized into a change event."""
    event = parse_github(_github_pr_payload())
    assert event is not None
    assert event.provider == "github"
    assert event.repo == "acme/app"
    assert event.ref == "7"
    assert event.head_sha == "abc123"
    assert event.pr_title == "Add endpoint"
    assert event.pr_body == "Adds the endpoint."


def test_parse_github_comment_builds_change_event() -> None:
    """GitHub @hermit comments on a pull request become change events."""
    payload = {
        "action": "created",
        "issue": {"number": 3, "pull_request": {}},
        "comment": {"body": "review this @or @hermit"},
        "repository": {"full_name": "acme/app"},
    }
    event = parse_github(payload)
    assert event is not None
    assert event.provider == "github"
    assert event.ref == "3"
    assert event.action == "comment"


def test_parse_github_comment_with_null_body() -> None:
    """GitHub @hermit comment on a PR with null body does not crash."""
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "title": "Add endpoint",
            "pull_request": {},
            "body": None,
            "html_url": "https://github.example/acme/app/pull/7",
        },
        "comment": {"body": "please @hermit review this", "user": {"login": "alice"}},
        "repository": {"full_name": "acme/app"},
    }
    event = parse_github(payload)
    assert event is not None
    assert event.pr_body == ""
    assert event.pr_title == "Add endpoint"
    assert event.ref == "7"
    assert event.action == "comment"


def test_parse_gitlab_returns_none_for_push() -> None:
    """GitLab payloads that are not merge requests are ignored."""
    assert parse_gitlab({"object_kind": "push"}) is None


def test_parse_gitlab_note_trigger() -> None:
    """GitLab notes on merge requests mentioning the bot become events."""
    payload = {
        "object_kind": "note",
        "object_attributes": {
            "noteable_type": "MergeRequest",
            "noteable_iid": 9,
            "note": "please @hermit review",
        },
        "merge_request": {
            "iid": 9,
            "title": "Add endpoint",
            "url": "https://gitlab.example/acme/app/-/merge_requests/9",
        },
        "project": {"path_with_namespace": "acme/app"},
    }
    event = parse_gitlab(payload)
    assert event is not None
    assert event.provider == "gitlab"
    assert event.ref == "9"
    assert event.action == "comment"


async def test_job_times_out_without_report() -> None:
    """A job that never reports is failed and cleaned up."""
    store = JobStore()
    spawner = FakePodSpawner()
    app = create_app(_settings(report_timeout_seconds=1), FakeClient(), spawner, store)
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=json.dumps(_github_pr_payload()).encode(),
            headers={
                "X-Hub-Signature-256": _github_signature(
                    json.dumps(_github_pr_payload()).encode()
                )
            },
        )
        assert response.status_code == 202
        job = store.all()[0]
        await asyncio.sleep(1.1)
    assert job.status == "failed"
    assert job.error == "review report timed out"
    assert spawner.cleaned == [job.id]
    assert not store.all()


async def test_github_ping_event_returns_ok() -> None:
    """GitHub ping events are acknowledged without spawning anything."""
    store = JobStore()
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), store)
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=b"{}",
            headers={"X-GitHub-Event": "ping"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not store.all()


async def test_webhook_rate_limit_returns_429() -> None:
    """Exceeding the per-IP limit rejects further requests."""
    store = JobStore()
    app = create_app(
        _settings(rate_limit_per_ip=2), FakeClient(), FakePodSpawner(), store
    )
    payload = _github_pr_payload()
    signed = _github_signature(json.dumps(payload).encode())
    async with _client(app) as http:
        first = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={"X-Hub-Signature-256": signed},
        )
        second = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={"X-Hub-Signature-256": signed},
        )
        third = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={"X-Hub-Signature-256": signed},
        )
    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 429


async def test_report_rejects_invalid_secret_before_reading_body() -> None:
    """An unauthenticated report is rejected before the body is parsed."""
    store = JobStore()
    app = create_app(_settings(), FakeClient(), FakePodSpawner(), store)
    job = await store.create(_github_event())
    async with _client(app) as http:
        response = await http.post(
            f"/internal/report/{job.id}",
            content=b"this is not json {",
            headers={"X-Hermit-Report-Secret": "wrong"},
        )
    assert response.status_code == 401


async def test_github_comment_from_non_member_is_rejected() -> None:
    """A comment from a user outside the org does not trigger a review."""
    store = JobStore()
    app = create_app(_settings(), NoMembershipClient(), FakePodSpawner(), store)
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "pull_request": {},
            "html_url": "https://github.example/acme/app/pull/7",
        },
        "comment": {
            "body": "please @hermit review",
            "user": {"login": "mallory"},
        },
        "repository": {"full_name": "acme/app"},
    }
    async with _client(app) as http:
        response = await http.post(
            "/webhook/github",
            content=json.dumps(payload).encode(),
            headers={
                "X-Hub-Signature-256": _github_signature(json.dumps(payload).encode())
            },
        )
    assert response.status_code == 403
    assert not store.all()


async def test_max_concurrent_jobs_rejects_excess() -> None:
    """A webhook beyond the concurrency limit is rejected with 503."""
    store = JobStore()
    spawner = FakePodSpawner()
    app = create_app(
        _settings(max_concurrent_jobs=2, report_timeout_seconds=10),
        FakeClient(),
        spawner,
        store,
    )
    payloads = []
    for sha in ("abc111", "abc222", "abc333"):
        payload = _github_pr_payload()
        payload["pull_request"]["head"]["sha"] = sha
        payloads.append(payload)
    async with _client(app) as http:
        statuses = []
        for payload in payloads:
            signed = _github_signature(json.dumps(payload).encode())
            response = await http.post(
                "/webhook/github",
                content=json.dumps(payload).encode(),
                headers={"X-Hub-Signature-256": signed},
            )
            statuses.append(response.status_code)
    assert statuses == [202, 202, 503]
    assert len(store.all()) == 2
