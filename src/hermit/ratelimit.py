"""Sliding-window request rate limiting for the webhook endpoints."""

import time
from typing import Any


class RateLimiter:
    """Tracks request timestamps per client IP and globally.

    A window of ``window`` seconds admits at most ``per_ip`` requests from a
    single source and ``global_limit`` requests overall. When
    ``trust_x_forwarded_for`` is True the real client IP is read from the
    rightmost entry of ``X-Forwarded-For`` (reverse-proxy chain); otherwise the
    direct connection address is used. Stale entries are pruned on every call
    to bound memory usage.
    """

    def __init__(
        self,
        window: float = 60.0,
        per_ip: int = 60,
        global_limit: int = 600,
        trust_x_forwarded_for: bool = False,
    ) -> None:
        self._window = window
        self._per_ip = per_ip
        self._global_limit = global_limit
        self._trust_xff = trust_x_forwarded_for
        self._ip_hits: dict[str, list[float]] = {}
        self._global_hits: list[float] = []

    def client_ip(self, request: Any) -> str:
        """Return the real client IP for ``request``."""
        if self._trust_xff:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        if request.client is not None:
            return request.client.host
        return "unknown"

    def _prune(self, now: float) -> None:
        """Drop timestamps that fell out of the sliding window."""
        cutoff = now - self._window
        self._ip_hits = {
            ip: [t for t in hits if t > cutoff]
            for ip, hits in self._ip_hits.items()
            if any(t > cutoff for t in hits)
        }
        self._global_hits = [t for t in self._global_hits if t > cutoff]

    def allow(self, request: Any) -> bool:
        """Record the request and return whether it is within the limits."""
        now = time.monotonic()
        self._prune(now)
        ip = self.client_ip(request)
        ip_hits = self._ip_hits.get(ip, [])
        ip_hits.append(now)
        self._global_hits.append(now)
        self._ip_hits[ip] = ip_hits
        return (
            len(ip_hits) <= self._per_ip
            and len(self._global_hits) <= self._global_limit
        )

    def ip_count(self) -> int:
        """Return the number of currently tracked source IPs."""
        return len(self._ip_hits)
