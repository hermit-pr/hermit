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
from hermit.verdict import (
    Review,
    VerdictError,
    inconclusive_review,
    nothing_to_review,
    parse_verdict,
    render_markdown,
)

logger = logging.getLogger(__name__)

#: Reminder appended to the original prompt when the model answered without a
#: parseable verdict. It is deliberately short and repeats the output contract.
JSON_ONLY_NUDGE = (
    "Your previous reply did not contain the required JSON verdict. Do not call "
    "any tools and do not add commentary. Reply now with exactly one ```json "
    "fenced object containing `summary`, `critical`, `medium`, `low`, and "
    "`verdict`, as specified in your instructions."
)


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

    An empty diff is returned as-is; the caller publishes a neutral
    "nothing to review" result instead of treating it as a pod failure.
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
        logger.info("no diff between base and head; the change may be merged")
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


_review_posted = [False]
_slave_settings = [None]


def _log_model_output(label: str, output: str) -> None:
    """Log a bounded tail of model output for post-mortem debugging."""
    logger.info("%s (%d bytes)\n%s", label, len(output), output[-4000:])


async def _collect_review(
    runner: OpenCodeRunner,
    prompt: str,
    diff: str,
    ignored_paths: tuple[str, ...],
    *,
    retry_timeout: float,
    output_tail_chars: int,
) -> Review:
    """Run the reviewer and turn its answer into a validated review.

    A model that ignores the output contract gets one retry with a JSON-only
    reminder and its own smaller deadline. If it still produces no parseable
    verdict, the review is inconclusive instead of failing the whole job: the
    model's answer is a review result, not an infrastructure error.
    """
    output = await runner.run(prompt)
    _log_model_output("review produced", output)
    try:
        return parse_verdict(output, diff, ignored_paths=ignored_paths)
    except VerdictError as exc:
        logger.warning("model output is not a valid verdict: %s", exc)
        logger.info(
            "retrying opencode once with a JSON-only reminder (timeout=%ss)",
            retry_timeout,
        )
        retry_output = await runner.run(
            f"{prompt}\n\n{JSON_ONLY_NUDGE}", timeout=retry_timeout
        )
        _log_model_output("retry produced", retry_output)
        try:
            return parse_verdict(retry_output, diff, ignored_paths=ignored_paths)
        except VerdictError as retry_exc:
            logger.warning("retry produced no valid verdict: %s", retry_exc)
            return inconclusive_review(
                retry_output or output,
                str(retry_exc),
                tail_chars=output_tail_chars,
            )


async def _publish_review(settings: SlaveSettings, review: Review) -> str:
    """Render ``review``, report it to the master, and return the posted body."""
    output = render_markdown(review)
    logger.info(
        "validated verdict %r with %d finding(s), %d downgraded, %d dropped",
        review.verdict,
        len(review.findings),
        review.relaxed,
        review.dropped,
    )
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
    _review_posted[0] = True
    return body


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
    if not diff.strip():
        logger.info("publishing a neutral nothing-to-review result")
        return await _publish_review(settings, nothing_to_review())
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
    review = await _collect_review(
        runner,
        prompt,
        diff,
        (policy_path,),
        retry_timeout=settings.opencode_retry_timeout_seconds,
        output_tail_chars=settings.review_output_tail_chars,
    )
    return await _publish_review(settings, review)


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
