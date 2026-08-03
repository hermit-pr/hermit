"""FastAPI application exposing the webhook and internal report endpoints."""

import asyncio
import hmac
import json
import logging
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hermit.config import Settings
from hermit.jobs import JobStore, ReviewJob
from hermit.k8s import PodSpawner, pod_environment
from hermit.models import ChangeEvent
from hermit.providers.base import GitClient
from hermit.signing import (
    WebhookValidationError,
    validate_github_signature,
    validate_gitlab_token,
)

logger = logging.getLogger(__name__)

GITHUB_ACTIONS = ("opened", "reopened", "synchronize")
GITLAB_ACTIONS = ("open", "reopen", "update")
BOT_MENTION = "@hermit"


def parse_github(payload: dict) -> Optional[ChangeEvent]:
    """Build a change event from a GitHub webhook payload.

    Handles pull request events and ``@hermit`` issue comments on pull requests.
    Returns ``None`` for payloads that should not trigger a review.
    """
    if "pull_request" in payload:
        return _parse_github_pull_request(payload)
    if payload.get("action") == "created" and payload.get("issue"):
        return _parse_github_comment(payload)
    return None


def _parse_github_pull_request(payload: dict) -> Optional[ChangeEvent]:
    """Parse a GitHub pull request event."""
    action = payload.get("action")
    if action not in GITHUB_ACTIONS:
        return None
    pull = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    return ChangeEvent(
        provider="github",
        action=action,
        repo=repository.get("full_name", ""),
        ref=str(pull.get("number", "")),
        head_sha=head.get("sha", ""),
        head_ref=head.get("ref", ""),
        base_sha=base.get("sha", ""),
        base_ref=base.get("ref", ""),
        title=pull.get("title", ""),
        url=pull.get("html_url", ""),
    )


def _parse_github_comment(payload: dict) -> Optional[ChangeEvent]:
    """Parse a GitHub issue comment that mentions the bot."""
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    if (
        "pull_request" not in issue
        or BOT_MENTION not in comment.get("body", "").lower()
    ):
        return None
    repository = payload.get("repository") or {}
    return ChangeEvent(
        provider="github",
        action="comment",
        repo=repository.get("full_name", ""),
        ref=str(issue.get("number", "")),
        title=issue.get("title", ""),
        url=issue.get("html_url", ""),
    )


def parse_gitlab(payload: dict) -> Optional[ChangeEvent]:
    """Build a change event from a GitLab webhook payload.

    Handles merge request events and ``@hermit`` notes on merge requests. Returns
    ``None`` for payloads that should not trigger a review.
    """
    if payload.get("object_kind") == "merge_request":
        return _parse_gitlab_merge_request(payload)
    if payload.get("object_kind") == "note":
        return _parse_gitlab_note(payload)
    return None


def _parse_gitlab_merge_request(payload: dict) -> Optional[ChangeEvent]:
    """Parse a GitLab merge request event."""
    attributes = payload.get("object_attributes") or {}
    action = attributes.get("action", "")
    if action not in GITLAB_ACTIONS:
        return None
    project = payload.get("project") or {}
    last_commit = attributes.get("last_commit") or {}
    return ChangeEvent(
        provider="gitlab",
        action=action,
        repo=project.get("path_with_namespace", ""),
        project_id=attributes.get("iid"),
        ref=str(attributes.get("iid", "")),
        head_sha=last_commit.get("id", ""),
        head_ref=attributes.get("source_branch", ""),
        base_ref=attributes.get("target_branch", ""),
        title=attributes.get("title", ""),
        url=attributes.get("url", ""),
    )


def _parse_gitlab_note(payload: dict) -> Optional[ChangeEvent]:
    """Parse a GitLab note on a merge request that mentions the bot."""
    attributes = payload.get("object_attributes") or {}
    if attributes.get("noteable_type") != "MergeRequest":
        return None
    if BOT_MENTION not in attributes.get("note", "").lower():
        return None
    project = payload.get("project") or {}
    merge_request = payload.get("merge_request") or {}
    ref = str(merge_request.get("iid", "") or attributes.get("noteable_iid", ""))
    return ChangeEvent(
        provider="gitlab",
        action="comment",
        repo=project.get("path_with_namespace", ""),
        project_id=merge_request.get("iid"),
        ref=ref,
        title=merge_request.get("title", ""),
        url=merge_request.get("url", ""),
    )


async def _watch_job(
    job: ReviewJob,
    spawner: PodSpawner,
    store: JobStore,
    timeout: float,
) -> None:
    """Wait for the reviewer pod's report, then clean up."""
    try:
        await asyncio.wait_for(job.reported.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        job.status = "failed"
        job.error = "review report timed out"
        logger.warning("job %s timed out", job.id)
    finally:
        await spawner.cleanup(job)
        await store.remove(job.id)


def create_app(
    settings: Settings,
    client: GitClient,
    spawner: PodSpawner,
    store: JobStore,
) -> FastAPI:
    """Build the FastAPI application wiring the provided dependencies."""
    secret = settings.webhook_secret.get_secret_value()
    tasks: set[asyncio.Task] = set()

    app = FastAPI(title="H.E.R.M.I.T", version="0.1.0")

    def track(job: ReviewJob) -> None:
        """Schedule the review watch for a spawned job."""
        task = asyncio.create_task(
            _watch_job(job, spawner, store, settings.report_timeout_seconds)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def accept(
        request: Request, provider: str, validate: Callable[[bytes], None]
    ) -> JSONResponse:
        """Validate a webhook and spawn a reviewer pod for the change."""
        body = await request.body()
        try:
            validate(body)
        except WebhookValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        payload = json.loads(body.decode("utf-8"))
        event = parse_github(payload) if provider == "github" else parse_gitlab(payload)
        if event is None:
            return JSONResponse({"status": "ignored"}, status_code=202)
        if not event.head_sha:
            try:
                event = await client.resolve_refs(event)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception(
                    "failed to resolve refs for %s %s#%s",
                    provider,
                    event.repo,
                    event.ref,
                )
                return JSONResponse(
                    {"error": "failed to resolve refs"}, status_code=502
                )
        job = await store.create(event)
        try:
            pod_name, secret_name = await spawner.spawn(
                job, pod_environment(settings, job)
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "failed to spawn reviewer pod for %s %s#%s",
                provider,
                event.repo,
                event.ref,
            )
            await store.remove(job.id)
            return JSONResponse({"error": "failed to spawn reviewer"}, status_code=503)
        job.pod_name = pod_name
        job.secret_name = secret_name
        track(job)
        return JSONResponse({"status": "accepted", "job_id": job.id}, status_code=202)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Return a liveness response for probes."""
        return {"status": "ok"}

    @app.post("/webhook/github")
    async def github_webhook(request: Request) -> JSONResponse:
        """Validate and accept a GitHub webhook request."""
        return await accept(
            request,
            "github",
            lambda body: validate_github_signature(
                secret, body, request.headers.get("x-hub-signature-256")
            ),
        )

    @app.post("/webhook/gitlab")
    async def gitlab_webhook(request: Request) -> JSONResponse:
        """Validate and accept a GitLab webhook request."""
        return await accept(
            request,
            "gitlab",
            lambda _body: validate_gitlab_token(
                secret, request.headers.get("x-gitlab-token")
            ),
        )

    @app.post("/internal/report/{job_id}")
    async def report(job_id: str, request: Request) -> JSONResponse:
        """Receive a review from a reviewer pod and submit it to the PR/MR."""
        job = await store.get(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        provided = request.headers.get("x-hermit-report-secret", "")
        if not hmac.compare_digest(provided, job.report_secret):
            return JSONResponse({"error": "invalid report secret"}, status_code=401)
        payload = await request.json()
        body = payload.get("body", "")
        if not body.strip():
            return JSONResponse({"error": "empty review body"}, status_code=400)
        try:
            await client.post_review(job.event, body)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "failed to submit review for %s %s#%s",
                job.event.provider,
                job.event.repo,
                job.event.ref,
            )
            job.status = "failed"
            job.error = "review submission failed"
            job.reported.set()
            return JSONResponse({"error": "review submission failed"}, status_code=502)
        job.body = body
        job.status = "reported"
        job.reported.set()
        return JSONResponse({"status": "ok"}, status_code=200)

    return app
