"""Runs the opencode agent as a subprocess."""

import asyncio
import os
from typing import List


class OpenCodeRunner:
    """Runs opencode against a vLLM endpoint for a given prompt."""

    def __init__(
        self,
        bin_path: str,
        args: List[str],
        endpoint: str,
        model: str,
        workspace: str,
    ) -> None:
        self._bin = bin_path
        self._args = args
        self._endpoint = endpoint
        self._model = model
        self._workspace = workspace

    def _environment(self) -> dict[str, str]:
        """Return the environment exported to the opencode process."""
        env = os.environ.copy()
        env["VLLM_ENDPOINT"] = self._endpoint
        env["MODEL"] = self._model
        return env

    async def run(self, prompt: str) -> str:
        """Execute opencode with ``prompt`` and return its output.

        Raises:
            RuntimeError: if opencode exits with a non-zero status.
        """
        command = [self._bin, *self._args, prompt]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._workspace,
            env=self._environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode().strip()
            raise RuntimeError(
                f"opencode failed with exit {process.returncode}: {message}"
            )
        return stdout.decode().strip()
