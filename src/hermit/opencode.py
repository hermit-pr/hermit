"""Runs the opencode agent as a subprocess."""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from hermit.config import DEFAULT_REVIEW_RULES

logger = logging.getLogger(__name__)

BASH_PERMISSIONS: dict[str, str] = {
    "git diff*": "allow",
    "git log*": "allow",
    "git show*": "allow",
    "git status*": "allow",
    "git branch*": "allow",
    "git fetch*": "allow",
    "git rev-parse*": "allow",
    "grep*": "allow",
    "find*": "allow",
    "cat*": "allow",
    "head*": "allow",
    "tail*": "allow",
    "ls*": "allow",
    "wc*": "allow",
    "sort*": "allow",
    "uniq*": "allow",
    "cut*": "allow",
    "tr*": "allow",
    "echo*": "allow",
    "which*": "allow",
    "pwd*": "allow",
    "env*": "allow",
    "date*": "allow",
    "printf*": "allow",
    "expr*": "allow",
    "test*": "allow",
    "true*": "allow",
    "false*": "allow",
    "dirname*": "allow",
    "basename*": "allow",
    "xargs*": "allow",
    "read": "allow",
    "*": "deny",
}


def extract_text(stdout: str) -> str:
    """Extract the final text block from opencode NDJSON output.

    Text fragments arrive as events with ``{"part": {"type": "text",
    "text": ...}}`` and are accumulated per message id. The concatenated text
    of the last message is returned. An ``error`` event raises ``RuntimeError``.

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
            raise RuntimeError(f"opencode returned an error event: {event}")
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        message_id = event.get("messageID") or event.get("messageId") or "default"
        messages.setdefault(str(message_id), []).append(text)
    if not messages:
        raise RuntimeError("opencode produced no text output")
    result = "".join(messages[list(messages)[-1]]).strip()
    result = re.sub(r"<thinking>.*?</thinking>", "", result, flags=re.DOTALL).strip()
    return result


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
    ) -> None:
        self._bin = bin_path
        self._endpoint = endpoint
        self._model = model
        self._workspace = workspace
        self._api_key = api_key
        self._extra_env = extra_env or {}
        self._review_rules = review_rules
        self._timeout = timeout

    def _write_config(self) -> None:
        """Write an opencode.json wiring the vLLM endpoint into the workspace.

        The permission block forbids any mutating shell command so that a
        review can only inspect the repository, never change it.

        Airgap safety: ``enabled_providers`` restricts opencode to only load
        the vLLM provider, preventing initialization of built-in providers
        that may attempt outbound connections. ``autoupdate`` and ``share``
        are explicitly disabled as defense-in-depth.
        """
        options: dict[str, object] = {"baseURL": self._endpoint}
        if self._api_key:
            options["apiKey"] = "{env:VLLM_API_KEY}"
        agent_prompt = DEFAULT_REVIEW_RULES
        if self._review_rules:
            agent_prompt += "\n" + self._review_rules
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"vllm/{self._model}",
            "default_agent": "hermit-reviewer",
            "autoupdate": False,
            "share": "disabled",
            "enabled_providers": ["vllm"],
            "disabled_providers": [
                "opencode",
                "anthropic",
                "openai",
                "google",
                "mistral",
                "groq",
                "github",
                "xai",
                "deepseek",
                "cohere",
                "together",
                "fireworks",
                "perplexity",
                "replicate",
                "openrouter",
                "voyage",
                "jina",
                "custom",
                "amazon-bedrock",
            ],
            "permission": {
                "bash": BASH_PERMISSIONS,
                "edit": "deny",
                "webfetch": "deny",
                "question": "deny",
            },
            "provider": {
                "vllm": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "H.E.R.M.I.T vLLM",
                    "options": options,
                    "models": {self._model: {"name": self._model, "id": self._model}},
                }
            },
            "agent": {
                "hermit-reviewer": {
                    "description": (
                        "H.E.R.M.I.T code reviewer — "
                        "evaluates pull requests for correctness, "
                        "security, and maintainability"
                    ),
                    "mode": "primary",
                    "temperature": 0.1,
                    "prompt": agent_prompt,
                    "permission": {
                        "edit": "deny",
                        "webfetch": "deny",
                        "question": "deny",
                        "recovery": "deny",
                        "task": {"*": "deny"},
                        "bash": BASH_PERMISSIONS,
                    },
                }
            },
        }
        config_path = Path(self._workspace) / "opencode.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.debug("wrote opencode config %s", config_path)

    def _environment(self) -> dict[str, str]:
        """Return the environment exported to the opencode process.

        ``OPENCODE_CONFIG`` pins the generated config file so a repository
        that ships its own ``opencode.json`` cannot override the vLLM endpoint
        (prompt injection via config smuggling).

        Airgap safety: several ``OPENCODE_DISABLE_*`` flags are set to prevent
        opencode from making outbound calls to models.dev, npm registries,
        update servers, or telemetry endpoints.
        """
        env = os.environ.copy()
        env.update(self._extra_env)
        env["VLLM_ENDPOINT"] = self._endpoint
        env["MODEL"] = self._model
        config_path = Path(self._workspace) / "opencode.json"
        env["OPENCODE_CONFIG"] = str(config_path)
        env["OPENCODE_CONFIG_DIR"] = self._workspace
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
        env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "1"
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
        prompt_path = Path(self._workspace) / "review-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [
            self._bin,
            "run",
            "--auto",
            "--print-logs",
            "--format",
            "json",
            str(prompt_path),
        ]
        logger.info(
            "running opencode %s run --auto --print-logs --format json (model=%s)",
            self._bin,
            self._model,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._workspace,
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
            logger.info("opencode stderr: %s", stderr.decode())
        if process.returncode != 0:
            message = stderr.decode().strip()
            logger.error("opencode failed: %s", message)
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
