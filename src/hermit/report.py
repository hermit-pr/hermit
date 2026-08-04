"""Reporting a review back to the master from the reviewer pod."""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


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
    logger.info("posting review report to %s", url)
    client = http or httpx.Client()
    try:
        response = client.post(url, json={"body": body}, headers=headers, timeout=120.0)
        logger.debug("report POST returned status %d", response.status_code)
        response.raise_for_status()
    finally:
        if http is None:
            client.close()
