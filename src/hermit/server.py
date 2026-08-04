"""FastAPI application exposing the webhook and internal report endpoints."""

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hermit import __version__
from hermit.config import Settings
from hermit.jobs import JobStore, ReviewJob
from hermit.k8s import (
    SWEEP_INTERVAL_SECONDS,
    JobAlreadyExists,
    PodSpawner,
    pod_environment,
)
from hermit.models import ChangeEvent
from hermit.providers.base import GitClient
from hermit.ratelimit import RateLimiter
from hermit.signing import (
    WebhookValidationError,
    validate_github_signature,
    validate_gitlab_token,
)

logger = logging.getLogger(__name__)

GITHUB_ACTIONS = ("opened", "reopened", "synchronize")
GITLAB_ACTIONS = ("open", "reopen", "update")
BOT_MENTION = "@hermit"
WATCH_POLL_SECONDS = 5.0


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
        repo=repository.get("full_name") or "",
        ref=str(pull.get("number") or ""),
        head_sha=head.get("sha") or "",
        head_ref=head.get("ref") or "",
        base_sha=base.get("sha") or "",
        base_ref=base.get("ref") or "",
        pr_title=pull.get("title") or "",
        pr_body=pull.get("body") or "",
        url=pull.get("html_url") or "",
    )


def _parse_github_comment(payload: dict) -> Optional[ChangeEvent]:
    """Parse a GitHub issue comment that mentions the bot."""
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    if (
        "pull_request" not in issue
        or BOT_MENTION not in (comment.get("body") or "").lower()
    ):
        return None
    repository = payload.get("repository") or {}
    return ChangeEvent(
        provider="github",
        action="comment",
        repo=repository.get("full_name") or "",
        ref=str(issue.get("number") or ""),
        pr_title=issue.get("title") or "",
        pr_body=issue.get("body") or "",
        url=issue.get("html_url") or "",
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
    action = attributes.get("action") or ""
    if action not in GITLAB_ACTIONS:
        return None
    project = payload.get("project") or {}
    last_commit = attributes.get("last_commit") or {}
    source = (attributes.get("source") or {}).get("path_with_namespace") or ""
    target = project.get("path_with_namespace") or ""
    source_repo = source if source and source != target else ""
    return ChangeEvent(
        provider="gitlab",
        action=action,
        repo=target,
        project_id=attributes.get("iid"),
        ref=str(attributes.get("iid") or ""),
        head_sha=last_commit.get("id") or "",
        head_ref=attributes.get("source_branch") or "",
        base_ref=attributes.get("target_branch") or "",
        pr_title=attributes.get("title") or "",
        pr_body=attributes.get("description") or "",
        url=attributes.get("url") or "",
        source_repo=source_repo,
    )


def _parse_gitlab_note(payload: dict) -> Optional[ChangeEvent]:
    """Parse a GitLab note on a merge request that mentions the bot."""
    attributes = payload.get("object_attributes") or {}
    if attributes.get("noteable_type") != "MergeRequest":
        return None
    if BOT_MENTION not in (attributes.get("note") or "").lower():
        return None
    project = payload.get("project") or {}
    merge_request = payload.get("merge_request") or {}
    ref = str(merge_request.get("iid") or attributes.get("noteable_iid") or "")
    return ChangeEvent(
        provider="gitlab",
        action="comment",
        repo=project.get("path_with_namespace") or "",
        project_id=merge_request.get("iid"),
        ref=ref,
        pr_title=merge_request.get("title") or "",
        pr_body=merge_request.get("description") or "",
        url=merge_request.get("url") or "",
    )


async def _watch_job(
    job: ReviewJob,
    spawner: PodSpawner,
    store: JobStore,
    timeout: float,
    client: GitClient,
) -> None:
    """Wait for the reviewer pod's report, then clean up.

    Completion is detected through the in-memory report event (fast path, same
    replica) or by polling the durable job status in Kubernetes (any replica,
    and after a master restart).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    try:
        while not job.reported.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                job.status = "failed"
                job.error = "review report timed out"
                logger.warning("job %s timed out after %.0fs", job.id, timeout)
                if job.event.head_sha:
                    try:
                        await client.set_commit_status(
                            job.event,
                            "error",
                            description="Review timed out.",
                            context="hermit/review",
                        )
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.warning(
                            "failed to set error status for %s#%s",
                            job.event.repo,
                            job.event.ref,
                        )
                break
            durable = await spawner.get_job(job.id)
            if durable is None or durable.status in ("posted", "failed"):
                job.status = durable.status if durable else "posted"
                logger.info("job %s finished; cleaning up", job.id)
                break
            try:
                await asyncio.wait_for(
                    job.reported.wait(), timeout=min(WATCH_POLL_SECONDS, remaining)
                )
            except asyncio.TimeoutError:
                pass
    finally:
        await spawner.cleanup(job)
        await store.remove(job.id)


async def _sweeper(spawner: PodSpawner) -> None:
    """Periodically reclaim orphaned reviewer pods and secrets."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await spawner.sweep()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("sweep of reviewer pods failed")


async def _evictor(store: JobStore) -> None:
    """Periodically evict jobs that leaked past their TTL."""
    while True:
        await asyncio.sleep(60)
        try:
            store.evict()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("eviction of stale jobs failed")


async def _decode_event(
    request: Request,
    provider: str,
    validate: Callable[[bytes], None],
    client: GitClient,
) -> tuple[Optional[ChangeEvent], Optional[JSONResponse]]:
    """Validate a webhook request and turn it into a change event.

    Returns ``(event, None)`` on success or ``(None, error_response)`` when the
    request must be rejected or ignored.
    """
    body = await request.body()
    try:
        validate(body)
    except WebhookValidationError as exc:
        logger.warning("rejected %s webhook: %s", provider, exc)
        return None, JSONResponse({"error": str(exc)}, status_code=401)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("rejected %s webhook with invalid JSON body", provider)
        return None, JSONResponse({"error": "invalid JSON body"}, status_code=400)
    event = parse_github(payload) if provider == "github" else parse_gitlab(payload)
    if event is None:
        logger.debug("ignoring %s webhook (no review triggered)", provider)
        return None, JSONResponse({"status": "ignored"}, status_code=202)
    logger.info(
        "webhook %s for %s#%s (action=%s)",
        provider,
        event.repo,
        event.ref,
        event.action,
    )
    if provider == "github" and event.action == "comment":
        await _authorize_github_commenter(payload, event, client)
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
            return None, JSONResponse(
                {"error": "failed to resolve refs"}, status_code=502
            )
    return event, None


async def _authorize_github_commenter(
    payload: dict, event: ChangeEvent, client: GitClient
) -> None:
    """Reject an ``@hermit`` comment from a user outside the org.

    The ``payload`` is the already-parsed webhook body, so the request stream
    is never read a second time.

    Raises:
        PermissionError: when the commenter is not a member of the org the
            repository belongs to and not a collaborator on the repo.
    """
    commenter = (payload.get("comment") or {}).get("user") or {}
    username = commenter.get("login") or ""
    owner = event.repo.split("/")[0] if "/" in event.repo else ""
    if not username:
        logger.warning("rejected @hermit comment: no username")
        raise PermissionError("commenter is not a repository member")
    is_member = await client.check_membership(owner, username)
    if not is_member:
        # Fallback: check if user is a collaborator on the repo
        # This handles outside collaborators and tokens with only 'repo' scope
        is_collaborator = await client.check_repo_collaborator(event.repo, username)
        if not is_collaborator:
            logger.warning(
                "rejected @hermit comment from non-member/non-collaborator %s on %s",
                username,
                event.repo,
            )
            raise PermissionError("commenter is not a repository member")


# pylint: disable=too-many-positional-arguments
async def _launch(
    provider: str,
    event: ChangeEvent,
    settings: Settings,
    spawner: PodSpawner,
    store: JobStore,
    track: Callable[[ReviewJob], None],
    client: GitClient,
) -> JSONResponse:
    """Create a job for ``event`` and spawn its reviewer pod."""
    if len(store.all()) >= settings.max_concurrent_jobs:
        logger.warning("max concurrent jobs reached (%d)", settings.max_concurrent_jobs)
        return JSONResponse({"error": "too many concurrent jobs"}, status_code=503)
    job = await store.create(event)
    logger.info(
        "starting hermit %s for %s %s#%s",
        job.id,
        event.provider,
        event.repo,
        event.ref,
    )
    try:
        pod_name, secret_name = await spawner.spawn(job, pod_environment(settings, job))
    except JobAlreadyExists:
        logger.info(
            "job %s already exists for %s %s#%s; ignoring duplicate",
            job.id,
            event.provider,
            event.repo,
            event.ref,
        )
        await store.remove(job.id)
        return JSONResponse({"status": "ignored", "job_id": job.id}, status_code=202)
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
    logger.info("starting pod %s for PR #%s repo %s", pod_name, event.ref, event.repo)
    track(job)
    if event.head_sha:
        try:
            await client.set_commit_status(
                event,
                "pending",
                description="H.E.R.M.I.T is reviewing...",
                context="hermit/review",
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "failed to set pending status for %s#%s", event.repo, event.ref
            )
    return JSONResponse({"status": "accepted", "job_id": job.id}, status_code=202)


# pylint: disable=too-many-locals, too-many-statements
def create_app(
    settings: Settings,
    client: GitClient,
    spawner: PodSpawner,
    store: JobStore,
) -> FastAPI:
    """Build the FastAPI application wiring the provided dependencies."""
    secret = settings.webhook_secret.get_secret_value()
    tasks: set[asyncio.Task] = set()
    limiter = RateLimiter(
        window=settings.rate_limit_window_seconds,
        per_ip=settings.rate_limit_per_ip,
        global_limit=settings.rate_limit_global,
    )

    def track(job: ReviewJob) -> None:
        """Schedule the review watch for a spawned job."""
        task = asyncio.create_task(
            _watch_job(job, spawner, store, settings.report_timeout_seconds, client)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Run a first sweep and keep reclaiming orphaned jobs on a timer."""
        try:
            await spawner.sweep()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("initial sweep failed")
        for task in (_sweeper(spawner), _evictor(store)):
            created = asyncio.create_task(task)
            tasks.add(created)
            created.add_done_callback(tasks.discard)
        yield
        await spawner.aclose()
        await client.aclose()

    app = FastAPI(title="H.E.R.M.I.T", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next) -> JSONResponse:
        """Reject requests whose declared body exceeds the size limit."""
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        return await call_next(request)

    # pylint: disable=too-many-return-statements
    async def accept(
        request: Request, provider: str, validate: Callable[[bytes], None]
    ) -> JSONResponse:
        """Validate a webhook and spawn a reviewer pod for the change."""
        logger.info("received webhook request (provider=%s)", provider)
        if not limiter.allow(request):
            logger.warning("rate limit exceeded for %s", limiter.client_ip(request))
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        event, error = await _decode_event(request, provider, validate, client)
        if error is not None:
            return error
        assert event is not None
        return await _launch(provider, event, settings, spawner, store, track, client)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Return a liveness response for probes."""
        return {"status": "ok", "version": __version__}

    @app.post("/webhook/github")
    async def github_webhook(request: Request) -> JSONResponse:
        """Validate and accept a GitHub webhook request.

        Ping events are acknowledged immediately without further processing.
        """
        if request.headers.get("x-github-event") == "ping":
            return JSONResponse({"status": "ok"}, status_code=200)
        try:
            return await accept(
                request,
                "github",
                lambda body: validate_github_signature(
                    secret, body, request.headers.get("x-hub-signature-256")
                ),
            )
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

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

    # pylint: disable=too-many-return-statements
    @app.post("/internal/report/{job_id}")
    async def report(job_id: str, request: Request) -> JSONResponse:
        """Receive a review from a reviewer pod and submit it to the PR/MR.

        The handler is stateless: it reads the job record from Kubernetes, so
        any replica can serve a report, including after a master restart. A
        ``posted`` status makes the endpoint idempotent.
        """
        if not limiter.allow(request):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        local = await store.get(job_id)
        job = local or await spawner.get_job(job_id)
        if job is None:
            logger.warning("report for unknown job %s", job_id)
            return JSONResponse({"error": "unknown job"}, status_code=404)
        provided = request.headers.get("x-hermit-report-secret", "")
        if not hmac.compare_digest(provided, job.report_secret):
            logger.warning("rejected report for job %s (invalid secret)", job_id)
            return JSONResponse({"error": "invalid report secret"}, status_code=401)
        if job.status == "posted":
            logger.info("report for job %s already posted; ignoring", job_id)
            return JSONResponse({"status": "ok"}, status_code=200)
        try:
            payload = await request.json()
        except Exception:  # pylint: disable=broad-exception-caught
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        body = payload.get("body", "")
        if not body.strip():
            logger.warning("rejected empty review body for job %s", job_id)
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
            try:
                await client.set_commit_status(
                    job.event,
                    "failure",
                    description="Review submission failed.",
                    context="hermit/review",
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "failed to set failure status for %s#%s",
                    job.event.repo,
                    job.event.ref,
                )
            job.status = "failed"
            job.error = "review submission failed"
            job.reported.set()
            await spawner.mark_failed(job_id)
            return JSONResponse({"error": "review submission failed"}, status_code=502)
        job.body = body
        job.status = "posted"
        job.reported.set()
        try:
            await client.set_commit_status(
                job.event,
                "success",
                description="Review completed.",
                context="hermit/review",
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "failed to set success status for %s#%s",
                job.event.repo,
                job.event.ref,
            )
        if await spawner.mark_posted(job_id):
            await spawner.cleanup(job)
        else:
            logger.info("job %s was already posted by another replica", job_id)
        logger.info(
            "received review from slave PR #%s repo %s",
            job.event.ref,
            job.event.repo,
        )
        return JSONResponse({"status": "ok"}, status_code=200)

    return app
