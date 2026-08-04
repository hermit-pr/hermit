"""Reviewer (slave) pod entrypoint: clone, diff, review, report."""

import asyncio
import logging
import os
import signal
import sys

import httpx

from hermit import __version__
from hermit.config import SlaveSettings
from hermit.git import (
    askpass_env,
    authenticated_url,
    clone_and_diff,
    extract_policy,
    write_askpass,
)
from hermit.logging_config import configure_logging
from hermit.opencode import OpenCodeRunner
from hermit.prompt import build_review_prompt
from hermit.report import report_failure, report_review
from hermit.secretscan import scan_for_secrets

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
    askpass_path = os.path.join(workspace, ".git-askpass")
    write_askpass(askpass_path)
    env = askpass_env(askpass_path)
    source = authenticated_url(
        settings.git_host_url, settings.repo, settings.git_provider
    )
    head_source_url = (
        authenticated_url(
            settings.git_host_url, settings.source_repo, settings.git_provider
        )
        if settings.source_repo
        else ""
    )
    diff = await asyncio.to_thread(
        clone_and_diff,
        settings.git_provider,
        source,
        repo_dir,
        settings.base_ref,
        settings.head_ref,
        base_sha=settings.base_sha,
        head_sha=settings.head_sha,
        pr_number=settings.ref if settings.git_provider == "github" else "",
        head_source_url=head_source_url,
        env=env,
    )
    try:
        os.remove(askpass_path)
    except OSError:
        pass
    if not diff.strip():
        raise ValueError("no diff between base and head")
    logger.info("computed diff of %d bytes", len(diff))
    policy_path = os.path.join(workspace, "policy.md")
    await asyncio.to_thread(
        extract_policy,
        repo_dir,
        settings.base_sha,
        policy_path,
        settings.policy_file_path,
    )
    logger.info("extracting policy file %s", settings.policy_file_path)
    prompt = build_review_prompt(
        settings.git_provider,
        settings.repo,
        settings.ref,
        settings.review_rules,
        diff,
        pr_title=settings.pr_title,
        pr_body=settings.pr_body,
        secret_candidates=scan_for_secrets(diff),
        policy_file=settings.policy_file_path,
        policy_extract_path=policy_path,
    )
    runner = OpenCodeRunner(
        settings.opencode_bin,
        settings.opencode_args,
        settings.vllm_endpoint,
        settings.model,
        workspace,
        api_key=(
            settings.vllm_api_key.get_secret_value() if settings.vllm_api_key else None
        ),
        extra_env={"PROJECT_POLICY_FILE": policy_path},
        timeout=settings.opencode_timeout_seconds,
    )
    logger.info(
        "invoking opencode against vLLM endpoint (model=%s)",
        settings.model,
    )
    output = await runner.run(prompt)
    logger.info("review produced (%d bytes); reporting to master", len(output))
    header = (
        f"## 🤖 H.E.R.M.I.T Code Review v{__version__}\n*Model: `{settings.model}`*\n\n"
    )
    body = header + output

    await report_review(
        settings.master_url,
        settings.job_id,
        settings.report_secret.get_secret_value(),
        body,
    )
    return body


def _handle_signal(signum: int, _frame: object) -> None:
    """Report a failure to the master before exiting on SIGTERM/SIGINT."""
    logger.warning("received signal %d, reporting failure", signum)
    try:
        settings = SlaveSettings()
    except (OSError, ValueError):
        logger.warning("cannot load settings in signal handler, exiting")
        sys.exit(1)
    report_failure(
        settings.master_url,
        settings.job_id,
        settings.report_secret.get_secret_value(),
        f"terminated by signal {signum}",
    )
    sys.exit(1)


def main() -> None:
    """Run a single review and exit with a status code."""
    configure_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(run_review(SlaveSettings()))
    except (RuntimeError, ValueError, OSError, httpx.HTTPError):
        logger.exception("review failed")
        raise SystemExit(1) from None
