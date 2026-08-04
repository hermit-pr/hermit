"""Git operations performed by the reviewer (slave) pod.

Authentication is provided through a ``GIT_ASKPASS`` helper: the clone URL never
embeds the token and the token never appears in the process argument list or in
logs.
"""

import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

BASE_TAG = "base-sha"


def _run(args: list[str], env: dict[str, str] | None = None) -> str:
    """Run a git command and return its stdout.

    Args are not logged because a clone URL may be sensitive.

    Raises:
        RuntimeError: if the command exits with a non-zero status.
    """
    logger.debug("running git command: %s", args[0])
    result = subprocess.run(args, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        message = result.stderr.strip()
        raise RuntimeError(f"{args[0]} failed: {message}")
    return result.stdout


def authenticated_url(host_url: str, repo: str, provider: str) -> str:
    """Build a clone URL for the given provider, without credentials.

    Only the username is embedded (``oauth2`` for GitLab, ``x-access-token`` for
    GitHub); the password (the read token) is supplied by ``GIT_ASKPASS``.
    """
    parsed = urlsplit(host_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    username = "oauth2" if provider == "gitlab" else "x-access-token"
    return f"{scheme}://{username}@{host}/{repo}.git"


def write_askpass(path: str) -> None:
    """Write a ``GIT_ASKPASS`` helper answering with ``HERMIT_GIT_READ_TOKEN``."""
    Path(path).write_text(
        '#!/bin/sh\necho "$HERMIT_GIT_READ_TOKEN"\n', encoding="utf-8"
    )
    os.chmod(path, 0o700)


def askpass_env(askpass_path: str) -> dict[str, str]:
    """Return the environment that routes git authentication to the helper."""
    env = os.environ.copy()
    env["GIT_ASKPASS"] = askpass_path
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def clone_repository(
    url: str, destination: str, env: dict[str, str] | None = None
) -> None:
    """Clone ``url`` into ``destination`` without a working tree checkout."""
    logger.info("cloning repository into %s", destination)
    _run(["git", "clone", "--no-checkout", url, destination], env=env)


def fetch_refs(
    repo_dir: str, refs: list[str], env: dict[str, str] | None = None
) -> None:
    """Fetch the given refs from the origin of ``repo_dir``."""
    logger.debug("fetching refs %s in %s", refs, repo_dir)
    _run(["git", "-C", repo_dir, "fetch", "origin", *refs], env=env)


def diff_between(repo_dir: str, base: str, head: str) -> str:
    """Return the unified diff between ``base`` and ``head``."""
    logger.debug("diffing %s..%s in %s", base, head, repo_dir)
    return _run(["git", "-C", repo_dir, "diff", "--no-color", f"{base}..{head}"])


def clone_and_diff(
    provider: str,
    source_url: str,
    repo_dir: str,
    base_ref: str,
    head_ref: str,
    *,
    base_sha: str = "",
    head_sha: str = "",
    pr_number: str = "",
    head_source_url: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Clone a repository and diff the exact base against the head.

    The repository is initialised empty and two shallow, depth-1 fetches pull
    the exact base commit and the head commit. The base commit is tagged
    ``base-sha`` so ``git diff base-sha`` always reflects the precise PR diff
    even if the target branch moved after the webhook fired.

    ``pr_number`` (GitHub) makes the head fetch use ``refs/pull/<n>/head`` so
    that pull requests from forks are supported. ``head_source_url`` (GitLab)
    makes the head fetch come from the fork project for cross-project merge
    requests. Exact SHAs take priority over branch names.
    """
    directory = Path(repo_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", str(directory)], env=env)
    _run(["git", "-C", str(directory), "remote", "add", "origin", source_url], env=env)
    base = base_sha or f"refs/heads/{base_ref}"
    _run(
        ["git", "-C", str(directory), "fetch", "--depth", "1", "origin", base],
        env=env,
    )
    if provider == "github" and pr_number:
        _run(
            [
                "git",
                "-C",
                str(directory),
                "fetch",
                "--depth",
                "1",
                "origin",
                f"refs/pull/{pr_number}/head",
            ],
            env=env,
        )
    elif head_source_url and head_source_url != source_url:
        _run(
            [
                "git",
                "-C",
                str(directory),
                "remote",
                "add",
                "head-source",
                head_source_url,
            ]
        )
        _run(
            [
                "git",
                "-C",
                str(directory),
                "fetch",
                "--depth",
                "1",
                "head-source",
                head_sha or f"refs/heads/{head_ref}",
            ],
            env=env,
        )
    else:
        _run(
            [
                "git",
                "-C",
                str(directory),
                "fetch",
                "--depth",
                "1",
                "origin",
                head_sha or f"refs/heads/{head_ref}",
            ],
            env=env,
        )
    _run(["git", "-C", str(directory), "checkout", "-q", "FETCH_HEAD"], env=env)
    base_object = base_sha or f"refs/remotes/origin/{base_ref}"
    _run(["git", "-C", str(directory), "tag", "-f", BASE_TAG, base_object], env=env)
    return _run(["git", "-C", str(directory), "diff", "--no-color", BASE_TAG], env=env)


def extract_policy(
    repo_dir: str, base: str, destination: str, policy_file: str = "AGENTS.md"
) -> bool:
    """Extract ``policy_file`` from ``base`` into ``destination``.

    Returns:
        True when the policy file exists at the base commit and was written.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            repo_dir,
            "show",
            f"{base}:{policy_file}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    Path(destination).write_text(result.stdout, encoding="utf-8")
    return True
