"""Abstract interface shared by all Git hosting providers."""

from abc import ABC, abstractmethod
from typing import Optional

import httpx

from hermit.models import ChangeEvent


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
        self._http = http or httpx.AsyncClient(
            base_url=self.endpoint, headers=self.headers()
        )

    @abstractmethod
    def headers(self) -> dict[str, str]:
        """Return the HTTP headers used to authenticate API requests."""

    @abstractmethod
    async def post_review(self, event: ChangeEvent, body: str) -> None:
        """Publish a review with ``body`` on the given change."""

    @abstractmethod
    async def resolve_refs(self, event: ChangeEvent) -> ChangeEvent:
        """Return the event with head/base refs filled from the provider API."""

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
