"""Runs the opencode agent as a subprocess.

The reviewer is driven by a generated *native OpenCode 2.x* configuration: the
agent may only read the checkout and run a small read-only shell allowlist, and
the vLLM endpoint is the single provider it can use.  The configuration is
pinned through ``OPENCODE_CONFIG`` with project configuration disabled and
every run uses ``--standalone``, so a configuration shipped by the reviewed
repository is never merged in: it could otherwise replace the reviewer's system
prompt, widen its permissions, or redirect inference to another endpoint (a
plain ``opencode run`` would also attach to the shared background server, which
ignores the per-invocation environment).
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from hermit.config import DEFAULT_REVIEW_RULES

logger = logging.getLogger(__name__)

PROVIDER_ID = "vllm"
PROVIDER_PACKAGE = "@opencode/ai/providers/openai-compatible"
AGENT_NAME = "hermit-reviewer"
MODEL_VARIANT = "hermit"
MODEL_TEMPERATURE = 0.1
TOOL_OUTPUT_MAX_LINES = 400
TOOL_OUTPUT_MAX_BYTES = 16384

#: Read-only shell commands the reviewer may run.  OpenCode 2 matches the
#: ``resource`` against the scanned command string and a trailing ``" *"`` also
#: matches the bare command.  The pull-request diff is injected into the prompt,
#: so shell access only exists to cross-check the target branch: nothing that
#: writes files, reaches the network, or spawns arbitrary processes is listed.
SHELL_ALLOWLIST: tuple[str, ...] = (
    "git diff *",
    "git log *",
    "git show *",
    "git status *",
    "git rev-parse *",
    "git branch *",
    "git describe *",
    "ls *",
)

#: Actions hard-denied to the reviewer.  ``edit`` covers write/patch,
#: ``subagent`` covers child agents, ``execute`` covers Code Mode.
DENIED_ACTIONS: tuple[str, ...] = (
    "edit",
    "webfetch",
    "websearch",
    "subagent",
    "question",
    "execute",
    "external_directory",
)


def shell_rules() -> list[dict[str, str]]:
    """Return ``shell`` permission rules allowing the read-only allowlist."""
    return [
        {"action": "shell", "resource": command, "effect": "allow"}
        for command in SHELL_ALLOWLIST
    ]


def permission_rules() -> list[dict[str, str]]:
    """Return the ordered permission rules for the reviewer agent.

    The first rule denies everything; later rules re-allow read-only discovery
    tools and the shell allowlist.  OpenCode resolves the *last* matching rule,
    so ordering matters and the deny-by-default rule must stay first.
    """
    rules = [{"action": "*", "resource": "*", "effect": "deny"}]
    for action in ("read", "glob", "grep"):
        rules.append({"action": action, "resource": "*", "effect": "allow"})
    rules.extend(shell_rules())
    return rules


def policy_rules() -> list[dict[str, str]]:
    """Return the policies that a reviewed repository cannot override.

    Policies are evaluated after project configuration, so these statements
    still hold when the checkout ships an ``opencode.json``.
    """
    policies = [
        {"action": "provider.use", "resource": "*", "effect": "deny"},
        {"action": "provider.use", "resource": PROVIDER_ID, "effect": "allow"},
        {"action": "integration.use", "resource": "mcp:*", "effect": "deny"},
        {"action": "integration.use", "resource": "skill:*", "effect": "deny"},
        {"action": "integration.use", "resource": "plugin:*", "effect": "deny"},
    ]
    for action in DENIED_ACTIONS:
        policies.append(
            {"action": "tool.use", "resource": f"{action}:*", "effect": "deny"}
        )
    # Shell is denied outright, then lifted command by command: an agent that
    # permits more (for example one defined by the repository) still cannot run
    # anything outside the allowlist.
    policies.append({"action": "tool.use", "resource": "shell:*", "effect": "deny"})
    for command in SHELL_ALLOWLIST:
        policies.append(
            {"action": "tool.use", "resource": f"shell:{command}", "effect": "allow"}
        )
    return policies


def _format_error(error: object) -> str:
    """Render an opencode error event payload for logs and exceptions.

    OpenCode 2.x emits a flat ``{"type": ..., "message": ...}`` object, while
    1.x nested ``name``/``data``; both are handled, and the raw payload is kept
    as a last resort so no diagnostic is lost.
    """
    if isinstance(error, dict):
        kind = error.get("type") or error.get("name")
        message = error.get("message") or error.get("data")
        if kind and message:
            return f"{kind}: {message}"
        if kind or message:
            return str(kind or message)
    return str(error)


def extract_text(stdout: str) -> str:
    """Extract all text blocks from opencode NDJSON output.

    Text fragments arrive as events with ``{"part": {"type": "text",
    "text": ...}}`` and are accumulated per message id. The concatenated text
    of every message, in insertion order, is returned. This preserves JSON
    verdicts emitted in an earlier message even when later messages add
    commentary. An ``error`` event raises ``RuntimeError``.

    Raises:
        RuntimeError: if opencode emitted an error event or no text at all.
    """
    messages: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            raise RuntimeError(
                f"opencode returned an error event: {_format_error(event.get('error'))}"
            )
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        message_id = (
            event.get("messageID")
            or event.get("messageId")
            or part.get("messageID")
            or "default"
        )
        messages.setdefault(str(message_id), []).append(text)
    if not messages:
        raise RuntimeError("opencode produced no text output")
    result = "".join("".join(texts) for texts in messages.values()).strip()
    result = re.sub(r"<thinking>.*?</thinking>", "", result, flags=re.DOTALL).strip()
    return result


def extract_json_verdict(output: str) -> str | None:
    """Parse the last JSON code block from *output* and reformat as markdown.

    Returns ``None`` if no valid JSON with a ``verdict`` key is found; the
    caller then fails the review instead of posting free-form model output.
    """
    blocks = re.findall(r"```json\s*(.*?)\s*```", output, re.DOTALL)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "verdict" not in data:
        return None
    lines: list[str] = []
    for severity in ("critical", "medium", "low"):
        findings = data.get(severity, [])
        if not findings:
            continue
        lines.append(f"## {severity.capitalize()} issues\n")
        for finding in findings:
            if isinstance(finding, dict):
                lines.append(
                    f"### {finding.get('title', 'Untitled')}\n\n"
                    f"{finding.get('detail', '')}\n\n"
                    f"**Fix**: {finding.get('fix', 'N/A')}\n"
                )
    lines.append(f"## Verdict\n\n**{data['verdict']}**")
    return "\n".join(lines)


class OpenCodeRunner:
    """Runs opencode against a vLLM endpoint for a given prompt."""

    def __init__(
        self,
        bin_path: str,
        endpoint: str,
        model: str,
        workspace: str,
        *,
        api_key: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        review_rules: str = "",
        timeout: int = 900,
        cwd: Optional[str] = None,
        max_steps: int = 12,
    ) -> None:
        self._bin = bin_path
        self._endpoint = endpoint
        self._model = model
        self._workspace = workspace
        self._api_key = api_key
        self._extra_env = extra_env or {}
        self._review_rules = review_rules
        self._timeout = timeout
        self._cwd = cwd
        self._max_steps = max_steps

    def _write_config(self) -> None:
        """Write an ``opencode.json`` wiring the vLLM endpoint into the workspace.

        The generated file uses the native OpenCode 2.x schema: ordered
        ``permissions``, non-overridable ``policies``, ``providers`` with an
        explicit model (vLLM discovery does not advertise tool support) and an
        ``agents`` entry carrying the system prompt and the step budget.

        Airgap safety: ``policies`` deny every provider except ``vllm`` and
        block MCP servers, skills, and plugins, while ``update`` is disabled and
        the built-in vLLM discovery plugin is turned off, so nothing is fetched
        from models.dev or a registry.
        """
        settings: dict[str, object] = {"baseURL": self._endpoint}
        if self._api_key:
            settings["apiKey"] = "{env:VLLM_API_KEY}"
        agent_prompt = DEFAULT_REVIEW_RULES
        if self._review_rules:
            agent_prompt += "\n" + self._review_rules
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{PROVIDER_ID}/{self._model}",
            "default_agent": AGENT_NAME,
            "update": "disable",
            "snapshots": False,
            "tool_output": {
                "max_lines": TOOL_OUTPUT_MAX_LINES,
                "max_bytes": TOOL_OUTPUT_MAX_BYTES,
            },
            "plugins": ["-opencode.provider.vllm"],
            "policies": policy_rules(),
            "permissions": permission_rules(),
            "providers": {
                PROVIDER_ID: {
                    "name": "H.E.R.M.I.T vLLM",
                    "package": PROVIDER_PACKAGE,
                    "settings": settings,
                    "models": {
                        self._model: {
                            "name": self._model,
                            "modelID": self._model,
                            "capabilities": {
                                "tools": True,
                                "input": ["text"],
                                "output": ["text"],
                            },
                            "variants": [
                                {
                                    "id": MODEL_VARIANT,
                                    "body": {"temperature": MODEL_TEMPERATURE},
                                }
                            ],
                        }
                    },
                }
            },
            "agents": {
                AGENT_NAME: {
                    "description": (
                        "H.E.R.M.I.T code reviewer — "
                        "evaluates pull requests for correctness, "
                        "security, and maintainability"
                    ),
                    "mode": "primary",
                    "steps": self._max_steps,
                    "model": f"{PROVIDER_ID}/{self._model}#{MODEL_VARIANT}",
                    "system": agent_prompt,
                    "permissions": permission_rules(),
                },
                # The hidden title agent would spend an extra model call per
                # review just to name a headless session.
                "title": {"disabled": True},
            },
        }
        config_path = Path(self._workspace) / "opencode.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.debug("wrote opencode config %s", config_path)

    def _environment(self) -> dict[str, str]:
        """Return the environment exported to the opencode process.

        ``OPENCODE_CONFIG`` pins the generated config file and project
        configuration is disabled, so a repository that ships its own
        ``opencode.json`` cannot override the vLLM endpoint, the reviewer's
        system prompt, or its permissions (prompt injection via config
        smuggling).  The pin only takes effect with ``--standalone``: without it
        the CLI attaches to the shared background server, which ignores
        per-invocation configuration.

        Airgap safety: update checks, model discovery, and free models are
        disabled so opencode never reaches out to models.dev, a registry, or an
        update server.
        """
        env = os.environ.copy()
        env.update(self._extra_env)
        env["VLLM_ENDPOINT"] = self._endpoint
        env["MODEL"] = self._model
        # subprocess(cwd=...) does not update PWD; opencode resolves the project
        # Location from it, so the reviewer would otherwise operate on the
        # master's directory instead of the clone.
        env["PWD"] = os.path.abspath(self._cwd or self._workspace)
        config_path = Path(self._workspace) / "opencode.json"
        env["OPENCODE_CONFIG"] = str(config_path)
        env["OPENCODE_CONFIG_DIR"] = self._workspace
        # The generated file outranks a repository-supplied ``opencode.json``,
        # but that file would still be merged in and could replace the agent's
        # system prompt (review integrity) or widen its permissions. Disable
        # project configuration outright; both variables are accepted by 2.x
        # and only the pinned configuration is loaded.
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        env["OPENCODE_DISABLE_FREE_MODELS"] = "1"
        if self._api_key:
            env["VLLM_API_KEY"] = self._api_key
        return env

    async def run(self, prompt: str) -> str:
        """Execute opencode with ``prompt`` and return its extracted output.

        The prompt is written to a file inside the workspace so it never
        appears in the process argument list (visible via ``/proc``) and is
        not limited by the OS argument length.

        Raises:
            RuntimeError: if opencode exits non-zero or emits no text.
        """
        self._write_config()
        await self._log_version()
        prompt_path = Path(self._workspace) / "review-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [
            self._bin,
            "--print-logs",
            "run",
            "--standalone",
            "--agent",
            AGENT_NAME,
            "--auto",
            "--format",
            "json",
            str(prompt_path),
        ]
        logger.info(
            "running opencode run --standalone --agent %s "
            "--auto --format json (model=%s, max_steps=%d)",
            AGENT_NAME,
            self._model,
            self._max_steps,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._cwd or self._workspace,
            env=self._environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"opencode timed out after {self._timeout}s") from None
        logger.debug("opencode exited with %d", process.returncode)
        if stderr:
            logger.debug("opencode stderr: %s", stderr.decode(errors="replace"))
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            logger.error("opencode failed: %s", message[-4000:])
            raise RuntimeError(
                f"opencode failed with exit {process.returncode}: {message}"
            )
        logger.info("opencode completed; output %d bytes", len(stdout))
        raw = stdout.decode("utf-8")
        logger.info("opencode raw output (%d bytes)\n%s", len(raw), raw[-8000:])
        try:
            log_path = os.path.join(
                os.path.expanduser("~"),
                ".local",
                "share",
                "opencode",
                "log",
                "opencode.log",
            )
            if os.path.isfile(log_path):
                with open(log_path, encoding="utf-8") as lf:
                    logger.info("opencode internal log:\n%s", lf.read()[-8000:])
        except OSError:
            pass
        return extract_text(raw)

    async def _log_version(self) -> None:
        """Log the opencode version, warning when it predates the 2.x schema."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except (OSError, asyncio.TimeoutError):
            logger.warning("could not determine the opencode version")
            return
        version = stdout.decode(errors="replace").strip().split()
        logger.info("opencode version: %s", " ".join(version) or "unknown")
        if not version:
            logger.warning("unrecognised opencode version output")
            return
        major = version[-1].lstrip("v").split(".", 1)[0]
        if not major.isdigit():
            logger.warning("unrecognised opencode version %r", " ".join(version))
            return
        if int(major) < 2:
            logger.warning(
                "opencode %s predates the 2.x schema hermit generates; upgrade "
                "the init image",
                version[-1],
            )
