"""Reporting a review back to the master from the reviewer pod."""

from typing import Optional

import httpx


def report_review(
    master_url: str,
    job_id: str,
    report_secret: str,
    body: str,
    http: Optional[httpx.Client] = None,
) -> None:
    """POST the review ``body`` to the master's internal report endpoint."""
    url = f"{master_url.rstrip('/')}/internal/report/{job_id}"
    headers = {"X-Hermit-Report-Secret": report_secret}
    client = http or httpx.Client()
    response = client.post(url, json={"body": body}, headers=headers, timeout=120.0)
    response.raise_for_status()
