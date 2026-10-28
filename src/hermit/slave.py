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
    diff_stat,
    ensure_commit,
    extract_policy,
    write_askpass,
)
from hermit.logging_config import configure_logging
from hermit.opencode import OpenCodeRunner
from hermit.prompt import build_review_prompt
from hermit.references import stale_references
from hermit.report import report_failure, report_review
from hermit.secretscan import scan_for_secrets
from hermit.verdict import parse_verdict, render_markdown

logger = logging.getLogger(__name__)


async def _prepare_repository(
    settings: SlaveSettings, workspace: str
) -> tuple[str, str, str, str, str]:
    """Clone the repository, compute the diff and extract the policy file.

    Returns a ``(diff, stat, policy_path, policy_text, repo_dir)`` tuple where
    ``stat`` is the ``git diff --stat`` summary, ``policy_path`` is where a copy
    of the policy was written for post-mortem, ``policy_text`` is the policy
    content quoted in the prompt (empty when the base commit has none) and
    ``repo_dir`` is the directory the repository was cloned into (used as the
    working directory for opencode).

    ``policy_path`` deliberately lives in the workspace, **outside** the clone:
    a policy copy inside the checkout would be matched by the rename ``grep``
    the reviewer runs, and hermit's own scratch file would be reported as a
    stale reference.

    Raises:
        ValueError: if the computed diff is empty.
    """
    repo_dir = os.path.join(workspace, "repo")
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
        head_sha=settings.head_sha,
        pr_number=settings.ref if settings.git_provider == "github" else "",
        head_source_url=head_source_url,
        env=env,
    )
    if not diff.strip():
        raise ValueError("no diff between base and head")
    logger.info("computed diff of %d bytes", len(diff))
    stat = await asyncio.to_thread(diff_stat, repo_dir)
    policy_path = os.path.join(workspace, ".hermit-policy.md")
    if settings.base_sha:
        await asyncio.to_thread(ensure_commit, repo_dir, settings.base_sha, env=env)
    try:
        os.remove(askpass_path)
    except OSError:
        pass
    policy_text = await asyncio.to_thread(
        extract_policy,
        repo_dir,
        settings.base_sha,
        policy_path,
        settings.policy_file_path,
    )
    if policy_text:
        logger.info("extracted policy file %s", settings.policy_file_path)
    else:
        logger.warning(
            "policy file %s not found at base commit, skipping",
            settings.policy_file_path,
        )
    return diff, stat, policy_path, policy_text, repo_dir


async def run_review(settings: SlaveSettings) -> str:
    """Perform one full review and return the review text."""
    workspace = os.path.abspath(settings.workspace)
    os.makedirs(workspace, exist_ok=True)
    logger.info(
        "starting review job %s for %s %s (%s) base=%s ref=%s",
        settings.job_id,
        settings.git_provider,
        settings.repo,
        settings.model,
        settings.base_ref,
        settings.head_ref,
    )
    diff, stat, policy_path, policy_text, repo_dir = await _prepare_repository(
        settings, workspace
    )
    references = await asyncio.to_thread(
        stale_references,
        diff,
        repo_dir,
        policy_file=settings.policy_file_path,
    )
    logger.info("%d verified stale reference(s) computed", len(references))
    prompt = build_review_prompt(
        settings.git_provider,
        settings.repo,
        settings.ref,
        pr_title=settings.pr_title,
        pr_body=settings.pr_body,
        secret_candidates=scan_for_secrets(diff),
        policy_file=settings.policy_file_path,
        policy_content=policy_text,
        stale_references=references,
        diff=diff,
        diff_stat=stat,
        max_diff_bytes=settings.max_prompt_diff_bytes,
    )
    runner = OpenCodeRunner(
        settings.opencode_bin,
        settings.vllm_endpoint,
        settings.model,
        workspace,
        api_key=(
            settings.vllm_api_key.get_secret_value() if settings.vllm_api_key else None
        ),
        extra_env={"PROJECT_POLICY_FILE": policy_path},
        review_rules=settings.review_rules,
        timeout=settings.opencode_timeout_seconds,
        cwd=repo_dir,
        max_steps=settings.review_max_steps,
    )
    logger.info(
        "invoking opencode against vLLM endpoint (model=%s, max_steps=%d)",
        settings.model,
        settings.review_max_steps,
    )
    output = await runner.run(prompt)
    logger.info("review produced (%d bytes); reporting to master", len(output))
    logger.info("raw opencode output (%d bytes)\n%s", len(output), output[-4000:])
    review = parse_verdict(output, diff, ignored_paths=(policy_path,))
    output = render_markdown(review)
    logger.info(
        "validated verdict %r with %d finding(s), dropped %d outside the diff",
        review.verdict,
        len(review.findings),
        review.dropped,
    )
    header = (
        f"## 🤖 H.E.R.M.I.T Code Review v{__version__}\n*Model: `{settings.model}`*\n\n"
    )

    await report_review(
        settings.master_url,
        settings.job_id,
        settings.report_secret.get_secret_value(),
        header + output,
    )
    _review_posted[0] = True
    return header + output


_review_posted = [False]
_slave_settings = [None]


def _handle_signal(signum: int, _frame: object) -> None:
    """Report a failure to the master before exiting on SIGTERM/SIGINT."""
    if _review_posted[0]:
        return
    logger.warning("received signal %d, reporting failure", signum)
    if _slave_settings[0] is None:
        logger.warning("settings not available in signal handler, exiting")
        sys.exit(1)
    report_failure(
        _slave_settings[0].master_url,
        _slave_settings[0].job_id,
        _slave_settings[0].report_secret.get_secret_value(),
        f"terminated by signal {signum}",
    )
    sys.exit(1)


def main() -> None:
    """Run a single review and exit with a status code."""
    configure_logging()
    settings = SlaveSettings()
    _slave_settings[0] = settings
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(run_review(settings))
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        logger.exception("review failed")
        report_failure(
            settings.master_url,
            settings.job_id,
            settings.report_secret.get_secret_value(),
            str(exc),
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
