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
import time
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

    def __init__(self, signing_key: str = "", ttl_seconds: int = 3600) -> None:
        self._jobs: dict[str, ReviewJob] = {}
        self._signing_key = signing_key
        self._created: dict[str, float] = {}
        self.ttl_seconds = ttl_seconds

    async def create(self, event: ChangeEvent) -> ReviewJob:
        """Create and record a job for ``event``.

        Returns the existing job if one with the same deterministic id already
        exists, so that duplicated webhooks never overwrite an in-flight job.
        """
        job_id = compute_job_id(event, self._signing_key)
        existing = self._jobs.get(job_id)
        if existing is not None:
            return existing
        job = ReviewJob(
            id=job_id,
            event=event,
            report_secret=secrets.token_urlsafe(32),
        )
        self._jobs[job.id] = job
        self._created[job.id] = time.monotonic()
        return job

    async def get(self, job_id: str) -> Optional[ReviewJob]:
        """Return the job with ``job_id`` or ``None``."""
        return self._jobs.get(job_id)

    async def remove(self, job_id: str) -> None:
        """Drop the job with ``job_id`` from the store."""
        self._jobs.pop(job_id, None)
        self._created.pop(job_id, None)

    def evict(self) -> None:
        """Drop jobs that have outlived the TTL without being cleaned up."""
        cutoff = time.monotonic() - self.ttl_seconds
        stale = [
            job_id for job_id, created in self._created.items() if created < cutoff
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)
            self._created.pop(job_id, None)

    def all(self) -> list[ReviewJob]:
        """Return a snapshot of all in-flight jobs."""
        return list(self._jobs.values())
