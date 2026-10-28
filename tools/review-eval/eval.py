"""Evaluate the review prompt against a real model.

The script replays hermit's production prompt and rules against small fixture
repositories and reports, per fixture, the verdict, the number of findings, how
many were dropped by the anchor check, and the number of tool calls the model
made. It is a tuning aid: run it against the models actually deployed and
compare the results after every prompt change.

Usage:
    python tools/review-eval/eval.py --model opencode-go/deepseek-v4.1-flash
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hermit.config import DEFAULT_REVIEW_RULES
from hermit.git import extract_policy
from hermit.opencode import (
    AGENT_NAME,
    TOOL_OUTPUT_MAX_BYTES,
    TOOL_OUTPUT_MAX_LINES,
    extract_text,
    permission_rules,
)
from hermit.prompt import build_review_prompt
from hermit.references import stale_references
from hermit.verdict import VerdictError, parse_verdict, render_markdown

DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: Policy copy location, mirroring ``hermit.slave``: outside the checkout.
POLICY_EXTRACT_NAME = ".hermit-policy.md"


def _git(repo: Path, *args: str) -> str:
    """Run a git command in *repo* and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _replace_tree(source: Path, destination: Path) -> None:
    """Replace everything except ``.git`` in *destination* with *source*."""
    for entry in destination.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _build_repo(fixture: Path, workdir: Path) -> tuple[str, str]:
    """Materialise the fixture as a base/head repository; return diff and stat."""
    repo = workdir / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "eval@example.com")
    _git(repo, "config", "user.name", "review eval")
    shutil.copytree(fixture / "base", repo, dirs_exist_ok=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "tag", "target-branch")
    _replace_tree(fixture / "head", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    diff = _git(repo, "diff", "--no-color", "target-branch")
    stat = _git(repo, "diff", "--no-color", "--stat", "target-branch")
    return diff, stat


def _write_config(workspace: Path, model: str, steps: int) -> Path:
    """Write an eval config using production rules but the requested model."""
    provider = model.split("/", 1)[0]
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "default_agent": AGENT_NAME,
        "update": "disable",
        "snapshots": False,
        "tool_output": {
            "max_lines": TOOL_OUTPUT_MAX_LINES,
            "max_bytes": TOOL_OUTPUT_MAX_BYTES,
        },
        "permissions": permission_rules(),
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": provider, "effect": "allow"},
        ],
        "agents": {
            AGENT_NAME: {
                "description": "H.E.R.M.I.T review evaluator",
                "mode": "primary",
                "steps": steps,
                "system": DEFAULT_REVIEW_RULES,
                "permissions": permission_rules(),
            },
            "title": {"disabled": True},
        },
    }
    path = workspace / "opencode.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _count_tool_calls(stdout: str) -> int:
    """Count tool events in the NDJSON stream."""
    calls = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") if isinstance(event, dict) else None
        if isinstance(part, dict) and part.get("type") == "tool":
            calls += 1
    return calls


def _run(
    opencode: str,
    repo: Path,
    workspace: Path,
    prompt: str,
    *,
    model: str,
    steps: int,
    timeout: int,
) -> tuple[str, int]:
    """Run opencode in *repo* and return (output text, tool call count)."""
    config = _write_config(workspace, model, steps)
    prompt_path = workspace / "review-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(config)
    env["OPENCODE_CONFIG_DIR"] = str(workspace)
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
    env["PWD"] = str(repo)
    result = subprocess.run(
        [
            opencode,
            "run",
            "--standalone",
            "--agent",
            AGENT_NAME,
            "--file",
            str(prompt_path),
            "--auto",
            "--format",
            "json",
            "Review the pull request in the attached file.",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"opencode exited {result.returncode}: {result.stderr.strip()[-800:]}"
        )
    return extract_text(result.stdout), _count_tool_calls(result.stdout)


def evaluate(
    fixture: Path, opencode: str, model: str, steps: int, timeout: int
) -> dict:
    """Run one fixture and return its result record.

    A fixture whose ``expect.json`` declares a ``policy`` file replays the
    production policy path: the file is extracted from the base commit into the
    workspace (never into the checkout) and quoted in the prompt, and findings
    naming that copy are dropped exactly as ``hermit.slave`` does.
    """
    expect = json.loads((fixture / "expect.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        repo = workdir / "repo"
        workspace = workdir / "workspace"
        workspace.mkdir()
        diff, stat = _build_repo(fixture, workdir)
        policy_path = workspace / POLICY_EXTRACT_NAME
        policy_content = ""
        policy_file = expect.get("policy", "AGENTS.md")
        if expect.get("policy"):
            policy_content = extract_policy(
                str(repo), "target-branch", str(policy_path), policy_file
            )
        references = stale_references(diff, str(repo), policy_file=policy_file)
        prompt = build_review_prompt(
            "gitlab",
            f"eval/{fixture.name}",
            "1",
            pr_title=expect.get("title", fixture.name),
            policy_file=policy_file,
            policy_content=policy_content,
            stale_references=references,
            diff=diff,
            diff_stat=stat,
        )
        output, tool_calls = _run(
            opencode,
            repo,
            workspace,
            prompt,
            model=model,
            steps=steps,
            timeout=timeout,
        )
    record = {
        "fixture": fixture.name,
        "expected": expect["verdict"],
        "tool_calls": tool_calls,
    }
    try:
        review = parse_verdict(output, diff, ignored_paths=(str(policy_path),))
    except VerdictError as exc:
        record.update({"status": "FAIL", "error": str(exc), "findings": 0})
        return record
    rendered = render_markdown(review)
    mentions = [m.lower() for m in expect.get("must_mention_any", [])]
    forbidden = [m.lower() for m in expect.get("must_not_mention", [])]
    record.update(
        {
            "verdict": review.verdict,
            "findings": len(review.findings),
            "dropped": review.dropped,
            "summary": review.summary,
            "mentions_expected": not mentions
            or any(m in rendered.lower() for m in mentions),
            "mentions_forbidden": any(m in rendered.lower() for m in forbidden),
            "markdown": rendered,
        }
    )
    checks = [
        review.verdict == expect["verdict"],
        len(review.findings) <= expect.get("max_findings", 99),
        record["mentions_expected"],
        not record["mentions_forbidden"],
    ]
    record["status"] = "PASS" if all(checks) else "FAIL"
    return record


def main() -> int:
    """Run every fixture and print a summary table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="opencode model reference")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--only", default="", help="run a single fixture")
    parser.add_argument("--json-out", default="", help="write the full report here")
    args = parser.parse_args()

    fixtures = sorted(
        path
        for path in Path(args.fixtures).iterdir()
        if path.is_dir() and (path / "expect.json").exists()
    )
    if args.only:
        fixtures = [path for path in fixtures if args.only in path.name]

    records = []
    for fixture in fixtures:
        print(f"… {fixture.name}", flush=True)
        try:
            record = evaluate(
                fixture, args.opencode_bin, args.model, args.steps, args.timeout
            )
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            record = {"fixture": fixture.name, "status": "ERROR", "error": str(exc)}
        records.append(record)
        detail = record.get("error") or (
            f"verdict={record.get('verdict')} findings={record.get('findings')} "
            f"dropped={record.get('dropped')} tools={record.get('tool_calls')}"
        )
        print(f"  {record['status']}: {detail}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(records, indent=2), encoding="utf-8")
    passed = sum(1 for record in records if record.get("status") == "PASS")
    print(f"\n{passed}/{len(records)} fixtures passed (model={args.model})")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
