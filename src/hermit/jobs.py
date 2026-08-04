"""In-memory tracking of review jobs.

Jobs are keyed by a deterministic id derived from the originating change event,
so that all master replicas agree on the same Kubernetes object names and a
duplicated webhook (or a webhook retried on another replica) maps to the same
job. The durable state of a job lives in Kubernetes (the per-job Secret); this
store is only a local cache used by the spawning replica's watcher.
"""

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Optional

from hermit.models import ChangeEvent

DEFAULT_SIGNING_KEY = "hermit"


def compute_job_id(event: ChangeEvent, signing_key: str) -> str:
    """Return the deterministic, DNS-safe id of the job for ``event``."""
    material = (
        f"{event.provider}|{event.repo}|{event.ref}|{event.action}|"
        f"{event.head_sha}|{event.base_sha}|{event.source_repo}"
    )
    digest = hmac.new(
        (signing_key or DEFAULT_SIGNING_KEY).encode(), material.encode(), hashlib.sha256
    ).hexdigest()
    return digest[:20]


@dataclass
class ReviewJob:
    """A single review in flight, tracked by the master."""

    id: str
    event: ChangeEvent
    report_secret: str
    status: str = "pending"
    pod_name: Optional[str] = None
    secret_name: Optional[str] = None
    body: Optional[str] = None
    error: Optional[str] = None
    reported: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class JobStore:
    """Holds the in-flight review jobs of a single master instance."""

    def __init__(self, signing_key: str = "") -> None:
        self._jobs: dict[str, ReviewJob] = {}
        self._signing_key = signing_key

    async def create(self, event: ChangeEvent) -> ReviewJob:
        """Create and record a job for ``event``."""
        job = ReviewJob(
            id=compute_job_id(event, self._signing_key),
            event=event,
            report_secret=secrets.token_urlsafe(32),
        )
        self._jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> Optional[ReviewJob]:
        """Return the job with ``job_id`` or ``None``."""
        return self._jobs.get(job_id)

    async def remove(self, job_id: str) -> None:
        """Drop the job with ``job_id`` from the store."""
        self._jobs.pop(job_id, None)

    def all(self) -> list[ReviewJob]:
        """Return a snapshot of all in-flight jobs."""
        return list(self._jobs.values())
