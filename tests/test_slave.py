"""Tests for the reviewer (slave) pod logic."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from hermit.git import (
    authenticated_url,
    clone_and_diff,
    clone_repository,
    diff_between,
    extract_policy,
    fetch_refs,
)
from hermit.prompt import build_review_prompt
from hermit.report import report_review


def test_authenticated_url_uses_github_username() -> None:
    """GitHub clone URLs use the x-access-token username and no token."""
    url = authenticated_url("https://git.example.com", "acme/app", "github")
    assert url == "https://x-access-token@git.example.com/acme/app.git"


def test_authenticated_url_uses_gitlab_username() -> None:
    """GitLab clone URLs use the oauth2 username and no token."""
    url = authenticated_url("https://git.example.com", "acme/app", "gitlab")
    assert url == "https://oauth2@git.example.com/acme/app.git"


def test_build_review_prompt_includes_rules_and_diff() -> None:
    """The prompt carries the rules, the diff and a clear instruction."""
    prompt = build_review_prompt(
        "github", "acme/app", "feature/x", "## Critical changes\n", "+code"
    )
    assert "You are H.E.R.M.I.T, a code reviewer" in prompt
    assert "git diff base-sha" in prompt
    assert "acme/app#feature/x" in prompt
    assert "## Critical changes\n" in prompt
    assert "+code" in prompt
    assert "GitHub-flavored Markdown" in prompt


def test_build_review_prompt_includes_secret_candidates() -> None:
    """The prompt surfaces regex-detected secret candidates for audit."""
    prompt = build_review_prompt(
        "github",
        "acme/app",
        "feature/x",
        "rules",
        "+code",
        secret_candidates=["GitHub Token: ghp_abcdefghijklmnopqrstuvwxyz123456"],
    )
    assert "secret_candidates" in prompt
    assert "GitHub Token" in prompt
    assert "TRUE POSITIVE" in prompt


async def test_report_review_posts_to_master() -> None:
    """The slave POSTs the review to the master report endpoint."""
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["secret"] = request.headers.get("x-hermit-report-secret")
        received["body"] = json.loads(request.content)["body"]
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await report_review(
            "http://hermit:8080",
            "job123",
            "s3cr3t",
            "review body",
            http=http,
        )
    assert received["url"] == "http://hermit:8080/internal/report/job123"
    assert received["secret"] == "s3cr3t"
    assert received["body"] == "review body"


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_git_clone_and_diff_returns_expected_output() -> None:
    """Clone, fetch and diff produce the change between two branches."""
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _run_git(["git", "init", "-b", "main", str(origin)])
        _run_git(["git", "config", "user.email", "test@example.com"], origin)
        _run_git(["git", "config", "user.name", "test"], origin)
        (origin / "file.txt").write_text("line1\n", encoding="utf-8")
        _run_git(["git", "add", "."], origin)
        _run_git(["git", "commit", "-m", "base"], origin)
        _run_git(["git", "checkout", "-b", "feature"], origin)
        (origin / "file.txt").write_text("line1\nline2\n", encoding="utf-8")
        _run_git(["git", "commit", "-am", "add line2"], origin)

        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        repo_dir = workspace / "repo"
        clone_repository(str(origin), str(repo_dir))
        fetch_refs(str(repo_dir), ["main", "feature"])
        diff = diff_between(str(repo_dir), "origin/main", "origin/feature")
        assert "+line2" in diff

        clone_and_diff(
            "github",
            str(origin),
            str(workspace / "repo2"),
            "main",
            "feature",
        )
        assert (workspace / "repo2").exists()


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_clone_and_diff_github_fork_uses_pull_ref() -> None:
    """GitHub fork PRs are fetched via refs/pull/<number>/head."""
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _run_git(["git", "init", "-b", "main", str(origin)])
        _run_git(["git", "config", "user.email", "test@example.com"], origin)
        _run_git(["git", "config", "user.name", "test"], origin)
        (origin / "file.txt").write_text("base\n", encoding="utf-8")
        _run_git(["git", "add", "."], origin)
        _run_git(["git", "commit", "-m", "base"], origin)
        _run_git(["git", "checkout", "-b", "feature"], origin)
        (origin / "file.txt").write_text("base\nhead\n", encoding="utf-8")
        _run_git(["git", "commit", "-am", "head"], origin)
        head_sha = _run_git(["git", "rev-parse", "HEAD"], origin).strip()
        _run_git(["git", "checkout", "main"], origin)
        _run_git(["git", "update-ref", "refs/pull/1/head", head_sha], origin)

        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        repo_dir = workspace / "repo"
        diff = clone_and_diff(
            "github",
            str(origin),
            str(repo_dir),
            "main",
            "feature",
            pr_number="1",
        )
        assert "+head" in diff


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_clone_and_diff_gitlab_cross_project_fork() -> None:
    """GitLab cross-project fork MRs fetch the head branch from the fork."""
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        fork = Path(tmp) / "fork"
        for path in (origin, fork):
            _run_git(["git", "init", "-b", "main", str(path)])
            _run_git(["git", "config", "user.email", "test@example.com"], path)
            _run_git(["git", "config", "user.name", "test"], path)
        (origin / "file.txt").write_text("base\n", encoding="utf-8")
        _run_git(["git", "add", "."], origin)
        _run_git(["git", "commit", "-m", "base"], origin)
        _run_git(["git", "remote", "add", "upstream", str(origin)], fork)
        _run_git(["git", "fetch", "upstream"], fork)
        _run_git(["git", "checkout", "-b", "feature", "upstream/main"], fork)
        (fork / "file.txt").write_text("base\nhead\n", encoding="utf-8")
        _run_git(["git", "commit", "-am", "head"], fork)

        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        repo_dir = workspace / "repo"
        diff = clone_and_diff(
            "gitlab",
            str(origin),
            str(repo_dir),
            "main",
            "feature",
            head_source_url=str(fork),
        )
        assert "+head" in diff


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_clone_and_diff_base_sha_takes_priority_over_base_ref() -> None:
    """The exact base SHA is used even when the target branch moved."""
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _run_git(["git", "init", "-b", "main", str(origin)])
        _run_git(["git", "config", "user.email", "test@example.com"], origin)
        _run_git(["git", "config", "user.name", "test"], origin)
        (origin / "file.txt").write_text("base\n", encoding="utf-8")
        _run_git(["git", "add", "."], origin)
        _run_git(["git", "commit", "-m", "base"], origin)
        base_sha = _run_git(["git", "rev-parse", "HEAD"], origin).strip()
        _run_git(["git", "checkout", "-b", "feature"], origin)
        (origin / "file.txt").write_text("base\nhead\n", encoding="utf-8")
        _run_git(["git", "commit", "-am", "head"], origin)
        head_sha = _run_git(["git", "rev-parse", "HEAD"], origin).strip()
        _run_git(["git", "checkout", "main"], origin)
        (origin / "file.txt").write_text("base\nmoved\n", encoding="utf-8")
        _run_git(["git", "commit", "-am", "target moved"], origin)

        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        repo_dir = workspace / "repo"
        diff = clone_and_diff(
            "github",
            str(origin),
            str(repo_dir),
            "main",
            "feature",
            base_sha=base_sha,
            head_sha=head_sha,
        )
        assert "+head" in diff
        assert "moved" not in diff


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_extract_policy_reads_file_from_base_commit() -> None:
    """The policy file is extracted from the base commit."""
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _run_git(["git", "init", "-b", "main", str(origin)])
        _run_git(["git", "config", "user.email", "test@example.com"], origin)
        _run_git(["git", "config", "user.name", "test"], origin)
        (origin / "AGENTS.md").write_text("# project rules\n", encoding="utf-8")
        _run_git(["git", "add", "."], origin)
        _run_git(["git", "commit", "-m", "base"], origin)
        base_sha = _run_git(["git", "rev-parse", "HEAD"], origin).strip()

        destination = Path(tmp) / "policy.md"
        got = extract_policy(str(origin), base_sha, str(destination))
        assert got is True
        assert destination.read_text(encoding="utf-8") == "# project rules\n"


def test_extract_policy_returns_false_when_absent() -> None:
    """A missing policy file is reported as not found."""
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _run_git(["git", "init", "-b", "main", str(origin)])
        _run_git(["git", "config", "user.email", "test@example.com"], origin)
        _run_git(["git", "config", "user.name", "test"], origin)
        (origin / "file.txt").write_text("x\n", encoding="utf-8")
        _run_git(["git", "add", "."], origin)
        _run_git(["git", "commit", "-m", "base"], origin)
        base_sha = _run_git(["git", "rev-parse", "HEAD"], origin).strip()

        destination = Path(tmp) / "policy.md"
        got = extract_policy(str(origin), base_sha, str(destination), "AGENTS.md")
        assert got is False
        assert not destination.exists()


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command in a temporary test repository and return stdout."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout
