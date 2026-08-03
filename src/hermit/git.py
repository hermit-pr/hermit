"""Git operations performed by the reviewer (slave) pod."""

import subprocess
from urllib.parse import quote, urlsplit


def _run(args: list[str]) -> str:
    """Run a git command and return its stdout.

    Raises:
        RuntimeError: if the command exits with a non-zero status.
    """
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = result.stderr.strip()
        raise RuntimeError(f"{args[0]} failed: {message}")
    return result.stdout


def authenticated_url(host_url: str, repo: str, provider: str, token: str) -> str:
    """Build a clone URL embedding ``token`` for the given provider."""
    parsed = urlsplit(host_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    username = "oauth2" if provider == "gitlab" else "x-access-token"
    return f"{scheme}://{username}:{quote(token, safe='')}@{host}/{repo}.git"


def clone_repository(url: str, destination: str) -> None:
    """Clone ``url`` into ``destination`` without a working tree checkout."""
    _run(["git", "clone", "--no-checkout", url, destination])


def fetch_refs(repo_dir: str, refs: list[str]) -> None:
    """Fetch the given refs from the origin of ``repo_dir``."""
    _run(["git", "-C", repo_dir, "fetch", "origin", *refs])


def diff_between(repo_dir: str, base: str, head: str) -> str:
    """Return the unified diff between ``base`` and ``head``."""
    return _run(["git", "-C", repo_dir, "diff", "--no-color", f"{base}..{head}"])


def clone_and_diff(
    source_url: str,
    repo_dir: str,
    base_ref: str,
    head_ref: str,
    base_sha: str = "",
    head_sha: str = "",
) -> str:
    """Clone a repository and diff the base against the head."""
    clone_repository(source_url, repo_dir)
    fetch_refs(repo_dir, [base_ref, head_ref])
    base = base_sha or f"origin/{base_ref}"
    head = head_sha or f"origin/{head_ref}"
    return diff_between(repo_dir, base, head)
