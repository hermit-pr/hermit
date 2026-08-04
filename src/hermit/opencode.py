"""Runs the opencode agent as a subprocess."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DENIED_PERMISSIONS = [
    "*git push*",
    "*git commit*",
    "*git remote*",
    "*git checkout -b*",
    "*git tag*",
    "*write*",
    "*mkdir*",
    "*edit*",
]
ALLOWED_PERMISSIONS = [
    "git diff*",
    "git log*",
    "git status*",
    "git show*",
    "git branch*",
    "git fetch*",
    "git rev-parse*",
    "ls*",
    "cat*",
    "grep*",
    "find*",
    "wc*",
    "read",
]


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
    return "".join(messages[list(messages)[-1]]).strip()


class OpenCodeRunner:
    """Runs opencode against a vLLM endpoint for a given prompt."""

    def __init__(
        self,
        bin_path: str,
        args: List[str],
        endpoint: str,
        model: str,
        workspace: str,
        *,
        api_key: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        timeout: int = 900,
    ) -> None:
        self._bin = bin_path
        self._args = args
        self._endpoint = endpoint
        self._model = model
        self._workspace = workspace
        self._api_key = api_key
        self._extra_env = extra_env or {}
        self._timeout = timeout

    def _write_config(self) -> None:
        """Write an opencode.json wiring the vLLM endpoint into the workspace.

        The permission block forbids any mutating shell command so that a
        review can only inspect the repository, never change it.
        """
        options: dict[str, object] = {"baseURL": self._endpoint}
        if self._api_key:
            options["apiKey"] = "{env:VLLM_API_KEY}"
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"vllm/{self._model}",
            "permission": {
                "deny": DENIED_PERMISSIONS,
                "allow": ALLOWED_PERMISSIONS,
            },
            "provider": {
                "vllm": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "H.E.R.M.I.T vLLM",
                    "options": options,
                    "models": {self._model: {"name": self._model}},
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
        """
        env = os.environ.copy()
        env.update(self._extra_env)
        env["VLLM_ENDPOINT"] = self._endpoint
        env["MODEL"] = self._model
        config_path = Path(self._workspace) / "opencode.json"
        env["OPENCODE_CONFIG"] = str(config_path)
        env["OPENCODE_CONFIG_DIR"] = self._workspace
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
        command = [self._bin, *self._args, str(prompt_path)]
        logger.info(
            "running opencode %s %s (model=%s)",
            self._bin,
            " ".join(self._args),
            self._model,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._workspace,
            env=self._environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=self._timeout
        )
        logger.debug("opencode exited with %d", process.returncode)
        if process.returncode != 0:
            message = stderr.decode().strip()
            logger.error("opencode failed: %s", message)
            raise RuntimeError(
                f"opencode failed with exit {process.returncode}: {message}"
            )
        logger.info("opencode completed; output %d bytes", len(stdout))
        return extract_text(stdout.decode("utf-8"))
