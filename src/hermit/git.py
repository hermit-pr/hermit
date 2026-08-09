"""Git operations performed by the reviewer (slave) pod.

Authentication is provided through a ``GIT_ASKPASS`` helper: the clone URL never
embeds the token and the token never appears in the process argument list or in
logs.
"""

import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

TARGET_TAG = "target-branch"
MAX_DIFF_BYTES = 100 * 1024 * 1024


def _run(
    args: list[str], env: dict[str, str] | None = None, max_bytes: int | None = None
) -> str:
    """Run a git command and return its stdout.

    Args are not logged because a clone URL may be sensitive.

    Args:
        args: The command and arguments.
        env: Environment dict.
        max_bytes: If set, limit stdout to this many bytes; raise RuntimeError
            if exceeded.

    Raises:
        RuntimeError: if the command exits with a non-zero status or exceeds
            ``max_bytes``.
    """
    logger.debug("running git command: %s", args[0])
    if max_bytes is None:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, env=env
        )
        if result.returncode != 0:
            message = result.stderr.strip()
            raise RuntimeError(f"{args[0]} failed: {message}")
        return result.stdout
    chunks: list[str] = []
    total = 0
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    ) as process:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                process.kill()
                raise RuntimeError(f"{args[0]} output exceeded {max_bytes} bytes")
            chunks.append(chunk)
        returncode = process.wait()
        if returncode != 0:
            stderr_output = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"{args[0]} failed: {stderr_output.strip()}")
    return "".join(chunks)


def authenticated_url(host_url: str, repo: str, provider: str) -> str:
    """Build a clone URL for the given provider, without credentials.

    Only the username is embedded (``oauth2`` for GitLab, ``x-access-token`` for
    GitHub); the password (the read token) is supplied by ``GIT_ASKPASS``.  The
    API path (e.g. ``/api/v3`` for GitHub Enterprise) is stripped from the URL
    so that the clone URL targets the git server rather than the REST API.
    """
    parsed = urlsplit(host_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    username = "oauth2" if provider == "gitlab" else "x-access-token"
    base = urlunsplit((scheme, f"{username}@{host}", "", "", ""))
    return f"{base}/{repo}.git"


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
    head_sha: str = "",
    pr_number: str = "",
    head_source_url: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Clone a repository and diff the PR head against the current target branch.

    The repository is initialised empty then shallow fetches pull the latest
    target-branch tip and the head commit.  The target branch is tagged
    ``target-branch`` and ``git diff target-branch`` (single-ref, comparing
    the working tree against the tag) isolates the PR's changes.  Because the
    tag always points at the current target-branch tip rather than a
    point-in-time fork sha, the diff is not polluted with upstream additions
    that arrived after the fork point, avoiding false-positive
    ``undefined reference`` hallucinations.

    ``pr_number`` (GitHub) makes the head fetch use ``refs/pull/<n>/head``.
    ``head_source_url`` (GitLab) fetches from the fork for cross-project MRs.
    Exact SHAs take priority over branch names for the head.
    """
    directory = Path(repo_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", str(directory)], env=env)
    _run(["git", "-C", str(directory), "remote", "add", "origin", source_url], env=env)
    _run(
        [
            "git",
            "-C",
            str(directory),
            "fetch",
            "--depth",
            "1",
            "origin",
            "--",
            f"refs/heads/{base_ref}",
        ],
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
            ],
            env=env,
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
                "--",
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
                "--",
                head_sha or f"refs/heads/{head_ref}",
            ],
            env=env,
        )
    _run(["git", "-C", str(directory), "checkout", "-q", "FETCH_HEAD"], env=env)
    _run(
        [
            "git",
            "-C",
            str(directory),
            "tag",
            "-f",
            TARGET_TAG,
            f"refs/remotes/origin/{base_ref}",
        ],
        env=env,
    )
    return _run(
        ["git", "-C", str(directory), "diff", "--no-color", TARGET_TAG],
        env=env,
        max_bytes=MAX_DIFF_BYTES,
    )


def ensure_commit(
    repo_dir: str, sha: str, *, env: dict[str, str] | None = None
) -> None:
    """Fetch *sha* from origin so it is present in *repo_dir*.

    Used by the policy-extraction path so that ``git show <sha>:<path>``
    works when the sha differs from the target-branch tip fetched by
    ``clone_and_diff``.
    """
    if not sha:
        return
    _run(
        [
            "git",
            "-C",
            str(repo_dir),
            "fetch",
            "--depth",
            "1",
            "origin",
            "--",
            sha,
        ],
        env=env,
    )


def extract_policy(
    repo_dir: str, base: str, destination: str, policy_file: str = "AGENTS.md"
) -> bool:
    """Extract ``policy_file`` from ``base`` into ``destination``.

    Returns:
        True when the policy file exists at the base commit and was written.
    """
    if base.startswith("-"):
        logger.error("rejecting policy extraction: base ref %r starts with '-'", base)
        return False
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
        timeout=30,
    )
    if result.returncode != 0:
        return False
    Path(destination).write_text(result.stdout, encoding="utf-8")
    return True
