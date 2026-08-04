"""Reviewer (slave) pod entrypoint: clone, diff, review, report."""

import asyncio
import logging
import os
import sys

from hermit.config import SlaveSettings
from hermit.git import authenticated_url, clone_and_diff
from hermit.opencode import OpenCodeRunner
from hermit.prompt import build_review_prompt
from hermit.report import report_review

logger = logging.getLogger(__name__)


async def run_review(settings: SlaveSettings) -> str:
    """Perform one full review and return the review text."""
    workspace = os.path.abspath(settings.workspace)
    os.makedirs(workspace, exist_ok=True)
    repo_dir = os.path.join(workspace, "repo")
    logger.info(
        "starting review job %s for %s %s (%s) base=%s ref=%s",
        settings.job_id,
        settings.git_provider,
        settings.repo,
        settings.model,
        settings.base_ref,
        settings.head_ref,
    )
    source = authenticated_url(
        settings.git_host_url,
        settings.repo,
        settings.git_provider,
        settings.git_read_token.get_secret_value(),
    )
    diff = clone_and_diff(
        source,
        repo_dir,
        settings.base_ref,
        settings.head_ref,
        settings.base_sha,
        settings.head_sha,
    )
    if not diff.strip():
        raise ValueError("no diff between base and head")
    logger.info("computed diff of %d bytes", len(diff))
    prompt = build_review_prompt(
        settings.git_provider,
        settings.repo,
        settings.head_ref,
        settings.review_rules,
        diff,
    )
    runner = OpenCodeRunner(
        settings.opencode_bin,
        settings.opencode_args,
        settings.vllm_endpoint,
        settings.model,
        workspace,
        settings.vllm_api_key.get_secret_value() if settings.vllm_api_key else None,
    )
    logger.info(
        "invoking opencode against vLLM endpoint (model=%s)",
        settings.model,
    )
    output = await runner.run(prompt)
    logger.info("review produced (%d bytes); reporting to master", len(output))
    report_review(
        settings.master_url,
        settings.job_id,
        settings.report_secret.get_secret_value(),
        output,
    )
    return output


def main() -> None:
    """Run a single review and exit with a status code."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        asyncio.run(run_review(SlaveSettings()))
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("review failed")
        raise SystemExit(1) from None
