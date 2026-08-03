"""In-memory tracking of review jobs."""

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Optional

from hermit.models import ChangeEvent


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

    def __init__(self) -> None:
        self._jobs: dict[str, ReviewJob] = {}

    async def create(self, event: ChangeEvent) -> ReviewJob:
        """Create and record a new job for ``event``."""
        job = ReviewJob(
            id=secrets.token_hex(6),
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
