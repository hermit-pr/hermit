"""Evaluate the review prompt against a real model.

The script replays hermit's production prompt and rules against small fixture
repositories and reports, per fixture, the verdict, the number of findings, how
many were dropped by the anchor check, and the number of tool calls the model
made. It is a tuning aid: run it against the models actually deployed and
compare the results after every prompt change.

Usage:
    # Production vLLM endpoint
    python tools/review-eval/eval.py \\
        --model Qwen/Qwen3-32B \\
        --endpoint "$HERMIT_VLLM_ENDPOINT"

    # Built-in OpenCode provider (benchmark/tuning on the public models)
    python tools/review-eval/eval.py \\
        --provider opencode \\
        --model nemotron-3.5-super-vl-free
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hermit.config import DEFAULT_REVIEW_RULES
from hermit.git import extract_policy
from hermit.opencode import (
    AGENT_NAME,
    MODEL_VARIANT,
    PROVIDER_ID,
    PROVIDER_PACKAGE,
    TOOL_OUTPUT_MAX_BYTES,
    TOOL_OUTPUT_MAX_LINES,
    extract_text,
    permission_rules,
    policy_rules,
)
from hermit.prompt import build_review_prompt
from hermit.references import stale_references
from hermit.verdict import VerdictError, parse_verdict, render_markdown

DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: Policy copy location, mirroring ``hermit.slave``: outside the checkout.
POLICY_EXTRACT_NAME = ".hermit-policy.md"
#: Evaluation runs are pinned to deterministic sampling: the goal is prompt
#: comparison, not creative variance.
EVAL_TEMPERATURE = 0.0


@dataclass(frozen=True)
class RunConfig:
    """Model and endpoint settings shared by every fixture in a run."""

    provider: str
    model: str
    steps: int
    timeout: int
    endpoint: str = ""
    api_key: str = ""


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


def _base_config(config: RunConfig, model_ref: str, agent_model: str) -> dict:
    """Return the settings shared by every eval provider."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model_ref,
        "default_agent": AGENT_NAME,
        "update": "disable",
        "snapshots": False,
        "tool_output": {
            "max_lines": TOOL_OUTPUT_MAX_LINES,
            "max_bytes": TOOL_OUTPUT_MAX_BYTES,
        },
        "permissions": permission_rules(),
        "agents": {
            AGENT_NAME: {
                "description": "H.E.R.M.I.T review evaluator",
                "mode": "primary",
                "steps": config.steps,
                "model": agent_model,
                "system": DEFAULT_REVIEW_RULES,
                "permissions": permission_rules(),
            },
            "title": {"disabled": True},
        },
    }


def _vllm_config(config: RunConfig) -> dict:
    """Build the production-equivalent vLLM provider configuration."""
    settings: dict[str, object] = {"baseURL": config.endpoint}
    if config.api_key:
        settings["apiKey"] = "{env:VLLM_API_KEY}"
    model_ref = f"{PROVIDER_ID}/{config.model}"
    payload = _base_config(config, model_ref, f"{model_ref}#{MODEL_VARIANT}")
    payload["plugins"] = ["-opencode.provider.vllm"]
    payload["policies"] = policy_rules()
    payload["providers"] = {
        PROVIDER_ID: {
            "name": "H.E.R.M.I.T vLLM (eval)",
            "package": PROVIDER_PACKAGE,
            "settings": settings,
            "models": {
                config.model: {
                    "name": config.model,
                    "modelID": config.model,
                    "capabilities": {
                        "tools": True,
                        "input": ["text"],
                        "output": ["text"],
                    },
                    "variants": [
                        {
                            "id": MODEL_VARIANT,
                            "body": {"temperature": EVAL_TEMPERATURE},
                        }
                    ],
                }
            },
        }
    }
    return payload


def _public_config(config: RunConfig) -> dict:
    """Build a config for a built-in provider such as ``opencode``."""
    model_ref = f"{config.provider}/{config.model}"
    payload = _base_config(config, model_ref, model_ref)
    payload["policies"] = [
        {"action": "provider.use", "resource": "*", "effect": "deny"},
        {"action": "provider.use", "resource": config.provider, "effect": "allow"},
    ]
    return payload


def _write_config(workspace: Path, config: RunConfig) -> Path:
    """Write an eval config that mirrors the production reviewer wiring.

    ``OPENCODE_CONFIG`` replaces the global configuration, so the generated
    file must contain everything the run needs: the vLLM provider block for an
    airgapped endpoint, or the allowlist for a built-in provider. Without it
    OpenCode falls back to an unrelated endpoint and every fixture fails with an
    empty stderr.
    """
    payload = (
        _vllm_config(config)
        if config.provider == PROVIDER_ID
        else _public_config(config)
    )
    path = workspace / "opencode.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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


def _error_detail(stdout: str, stderr: str) -> str:
    """Return the most useful diagnostic from an opencode run.

    A provider error (rate limit, invalid model) is emitted as an NDJSON
    ``{"type": "error"}`` event with an empty stderr, so the raw stderr alone
    hides the cause.
    """
    if stderr.strip():
        return stderr.strip()[-800:]
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            return json.dumps(event.get("error", event))[-800:]
    return stdout.strip()[-800:]


def _run(
    opencode: str,
    repo: Path,
    workspace: Path,
    prompt: str,
    *,
    config: RunConfig,
) -> tuple[str, int]:
    """Run opencode in *repo* and return (output text, tool call count)."""
    config_path = _write_config(workspace, config)
    prompt_path = workspace / "review-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(config_path)
    env["OPENCODE_CONFIG_DIR"] = str(workspace)
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    if config.provider == PROVIDER_ID:
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        env["OPENCODE_DISABLE_FREE_MODELS"] = "1"
        if config.api_key:
            env["VLLM_API_KEY"] = config.api_key
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
        timeout=config.timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"opencode exited {result.returncode}: "
            f"{_error_detail(result.stdout, result.stderr)}"
        )
    return extract_text(result.stdout), _count_tool_calls(result.stdout)


def _prepare_fixture(
    fixture: Path, workdir: Path, workspace: Path
) -> tuple[dict, str, str, Path]:
    """Materialize the fixture and build the production review prompt.

    Returns ``(expect, diff, prompt, policy_path)``. A fixture whose
    ``expect.json`` declares a ``policy`` file replays the production policy
    path: the file is extracted from the base commit into the workspace (never
    into the checkout) and quoted in the prompt.
    """
    expect = json.loads((fixture / "expect.json").read_text(encoding="utf-8"))
    repo = workdir / "repo"
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
    return expect, diff, prompt, policy_path


def _evaluate_output(
    record: dict, output: str, diff: str, policy_path: Path, expect: dict
) -> None:
    """Parse the review, render it, and fill the result record."""
    try:
        review = parse_verdict(output, diff, ignored_paths=(str(policy_path),))
    except VerdictError as exc:
        record.update({"status": "FAIL", "error": str(exc), "findings": 0})
        return
    rendered = render_markdown(review)
    mentions = [m.lower() for m in expect.get("must_mention_any", [])]
    forbidden = [m.lower() for m in expect.get("must_not_mention", [])]
    record.update(
        {
            "verdict": review.verdict,
            "findings": len(review.findings),
            "dropped": review.dropped,
            "relaxed": review.relaxed,
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


def evaluate(fixture: Path, opencode: str, config: RunConfig) -> dict:
    """Run one fixture and return its result record."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        workspace = workdir / "workspace"
        workspace.mkdir()
        expect, diff, prompt, policy_path = _prepare_fixture(
            fixture, workdir, workspace
        )
        output, tool_calls = _run(
            opencode,
            workdir / "repo",
            workspace,
            prompt,
            config=config,
        )
    record = {
        "fixture": fixture.name,
        "expected": expect["verdict"],
        "tool_calls": tool_calls,
    }
    _evaluate_output(record, output, diff, policy_path, expect)
    return record


def main() -> int:
    """Run every fixture and print a summary table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        default=PROVIDER_ID,
        help=(
            "opencode provider id: 'vllm' (default), or a built-in provider "
            "such as 'opencode' or 'opencode-go'"
        ),
    )
    parser.add_argument(
        "--model", required=True, help="model id without the provider prefix"
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("HERMIT_VLLM_ENDPOINT", ""),
        help="vLLM endpoint (default: HERMIT_VLLM_ENDPOINT; vllm provider only)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("HERMIT_VLLM_API_KEY", ""),
        help="vLLM API key (default: HERMIT_VLLM_API_KEY; vllm provider only)",
    )
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--steps", type=int, default=24)
    parser.add_argument("--only", default="", help="run a single fixture")
    parser.add_argument("--json-out", default="", help="write the full report here")
    args = parser.parse_args()
    if args.provider == PROVIDER_ID and not args.endpoint:
        parser.error("--endpoint is required for the vllm provider")
    model = args.model
    if args.provider != PROVIDER_ID and model.startswith(f"{args.provider}/"):
        model = model.split("/", 1)[1]

    fixtures = sorted(
        path
        for path in Path(args.fixtures).iterdir()
        if path.is_dir() and (path / "expect.json").exists()
    )
    if args.only:
        fixtures = [path for path in fixtures if args.only in path.name]

    config = RunConfig(
        provider=args.provider,
        model=model,
        steps=args.steps,
        timeout=args.timeout,
        endpoint=args.endpoint,
        api_key=args.api_key,
    )
    records = []
    for fixture in fixtures:
        print(f"… {fixture.name}", flush=True)
        try:
            record = evaluate(fixture, args.opencode_bin, config)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            record = {"fixture": fixture.name, "status": "ERROR", "error": str(exc)}
        records.append(record)
        detail = record.get("error") or (
            f"verdict={record.get('verdict')} findings={record.get('findings')} "
            f"dropped={record.get('dropped')} relaxed={record.get('relaxed')} "
            f"tools={record.get('tool_calls')}"
        )
        print(f"  {record['status']}: {detail}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(records, indent=2), encoding="utf-8")
    passed = sum(1 for record in records if record.get("status") == "PASS")
    print(f"\n{passed}/{len(records)} fixtures passed (model={args.model})")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
