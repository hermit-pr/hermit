"""Abstract interface shared by all Git hosting providers."""

import asyncio
import logging
import os
import random
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from hermit.models import ChangeEvent

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
MAX_BACKOFF_SECONDS = 8.0


def _backoff(attempt: int) -> float:
    """Return an exponentially growing delay with jitter for ``attempt``."""
    base = min(2**attempt, MAX_BACKOFF_SECONDS)
    return base + random.uniform(0, 0.5)


class GitClient(ABC):
    """Thin authenticated client over a Git hosting provider API."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        if http is not None:
            self._http = http
            return
        verify: bool | str = True
        for env_var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            cert_path = os.environ.get(env_var)
            if cert_path and os.path.isfile(cert_path):
                verify = cert_path
                break
        self._http = httpx.AsyncClient(
            base_url=self.endpoint,
            headers=self.headers(),
            verify=verify,
            follow_redirects=True,
        )

    @abstractmethod
    def headers(self) -> dict[str, str]:
        """Return the HTTP headers used to authenticate API requests."""

    @abstractmethod
    async def post_review(self, event: ChangeEvent, body: str) -> None:
        """Publish a review with ``body`` on the given change."""

    @abstractmethod
    async def set_commit_status(
        self, event: ChangeEvent, state: str, description: str, context: str
    ) -> None:
        """Set a commit status (pending/success/failure/error) on the head commit."""

    @abstractmethod
    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Return the event with head/base refs filled from the provider API."""

    async def check_membership(self, _org: str, _username: str) -> bool:
        """Return True when ``username`` is a member of ``org``."""
        return True

    async def check_repo_collaborator(self, _repo: str, _username: str) -> bool:
        """Return True when ``username`` is a collaborator on ``repo``.

        Used as fallback when org membership check fails or is unavailable.
        Only requires ``repo`` scope on GitHub.
        """
        return True

    async def _request(
        self, method: str, path: str, **kwargs: object
    ) -> httpx.Response:
        """Perform an HTTP request with retries on transient failures.

        Retries on connection errors, timeouts, 429 (honoring ``Retry-After``)
        and 5xx responses with exponential backoff and jitter. Non-retryable
        4xx responses are returned immediately.
        """
        attempt = 0
        while True:
            try:
                response = await self._http.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt >= MAX_RETRIES:
                    raise
                delay = _backoff(attempt)
                logger.debug(
                    "retrying %s %s after transport error in %.1fs", method, path, delay
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if response.status_code == 429:
                if attempt >= MAX_RETRIES:
                    return response
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = _backoff(attempt)
                else:
                    delay = _backoff(attempt)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if response.status_code >= 500:
                if attempt >= MAX_RETRIES:
                    return response
                delay = _backoff(attempt)
                logger.debug(
                    "retrying %s %s after status %d in %.1fs",
                    method,
                    path,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            return response

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
