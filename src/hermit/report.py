"""Reporting a review back to the master from the reviewer pod."""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _ssl_verify() -> bool | str:
    """Return the SSL verification path from environment, or True."""
    for env_var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        cert_path = os.environ.get(env_var)
        if cert_path and os.path.isfile(cert_path):
            return cert_path
    return True


async def report_review(
    master_url: str,
    job_id: str,
    report_secret: str,
    body: str,
    http: Optional[httpx.AsyncClient] = None,
) -> None:
    """POST the review ``body`` to the master's internal report endpoint."""
    url = f"{master_url.rstrip('/')}/internal/report/{job_id}"
    headers = {"X-Hermit-Report-Secret": report_secret}
    logger.info("posting review report to %s", url)
    client = http or httpx.AsyncClient(verify=_ssl_verify())
    try:
        response = await client.post(
            url, json={"body": body, "success": True}, headers=headers, timeout=120.0
        )
        logger.debug("report POST returned status %d", response.status_code)
        response.raise_for_status()
    finally:
        if http is None:
            await client.aclose()


def report_failure(
    master_url: str,
    job_id: str,
    report_secret: str,
    error: str,
) -> None:
    """Best-effort, synchronous failure report for use in signal handlers.

    Runs in the main thread of a terminating process, so it uses a plain
    ``httpx.Client`` and swallows all errors.
    """
    url = f"{master_url.rstrip('/')}/internal/report/{job_id}"
    headers = {"X-Hermit-Report-Secret": report_secret}
    try:
        with httpx.Client(verify=_ssl_verify()) as client:
            response = client.post(
                url,
                json={"body": f"Review failed: {error}", "success": False},
                headers=headers,
                timeout=30.0,
            )
            logger.debug("failure report returned status %d", response.status_code)
    except httpx.HTTPError:
        logger.debug("could not report failure for job %s", job_id)
