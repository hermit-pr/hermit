"""Runs the opencode agent as a subprocess."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class OpenCodeRunner:
    """Runs opencode against a vLLM endpoint for a given prompt."""

    def __init__(
        self,
        bin_path: str,
        args: List[str],
        endpoint: str,
        model: str,
        workspace: str,
        api_key: Optional[str] = None,
    ) -> None:
        self._bin = bin_path
        self._args = args
        self._endpoint = endpoint
        self._model = model
        self._workspace = workspace
        self._api_key = api_key

    def _write_config(self) -> None:
        """Write an opencode.json wiring the vLLM endpoint into the workspace."""
        options: dict[str, object] = {"baseURL": self._endpoint}
        if self._api_key:
            options["apiKey"] = "{env:VLLM_API_KEY}"
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"vllm/{self._model}",
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
        """Return the environment exported to the opencode process."""
        env = os.environ.copy()
        env["VLLM_ENDPOINT"] = self._endpoint
        env["MODEL"] = self._model
        if self._api_key:
            env["VLLM_API_KEY"] = self._api_key
        return env

    async def run(self, prompt: str) -> str:
        """Execute opencode with ``prompt`` and return its output.

        Raises:
            RuntimeError: if opencode exits with a non-zero status.
        """
        self._write_config()
        command = [self._bin, *self._args, prompt]
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
        stdout, stderr = await process.communicate()
        logger.debug("opencode exited with %d", process.returncode)
        if process.returncode != 0:
            message = stderr.decode().strip()
            logger.error("opencode failed: %s", message)
            raise RuntimeError(
                f"opencode failed with exit {process.returncode}: {message}"
            )
        logger.info("opencode completed; output %d bytes", len(stdout))
        return stdout.decode().strip()
