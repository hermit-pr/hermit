"""Tests for the opencode integration."""

import json
import tempfile
from pathlib import Path

from hermit.opencode import OpenCodeRunner


def _run(opencode_opts: dict) -> dict:
    """Generate and read back the opencode config for the given options."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            args=["run"],
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
            **opencode_opts,
        )
        runner._write_config()  # pylint: disable=protected-access
        config_path = Path(tmp) / "opencode.json"
        assert config_path.exists()
        return json.loads(config_path.read_text(encoding="utf-8"))


def test_opencode_config_wires_endpoint_and_model() -> None:
    """The generated opencode.json targets the vLLM endpoint and model."""
    config = _run({})
    provider = config["provider"]["vllm"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://vllm.example:8000/v1"
    assert config["model"] == "vllm/llama-3.1-8b-instruct"
    assert "llama-3.1-8b-instruct" in provider["models"]


def test_opencode_config_omits_api_key_when_absent() -> None:
    """Without a key, the config does not set an apiKey."""
    config = _run({})
    assert "apiKey" not in config["provider"]["vllm"]["options"]


def test_opencode_config_references_key_by_env_when_present() -> None:
    """With a key, the config references it via env var, never in plaintext."""
    config = _run({"api_key": "sk-secret"})
    options = config["provider"]["vllm"]["options"]
    assert options["apiKey"] == "{env:VLLM_API_KEY}"
    assert "sk-secret" not in json.dumps(options)


def test_opencode_environment_exports_api_key_when_present() -> None:
    """The opencode process gets the API key through the environment."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            args=["run"],
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
            api_key="sk-secret",
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert env["VLLM_API_KEY"] == "sk-secret"
        assert env["VLLM_ENDPOINT"] == "http://vllm.example:8000/v1"
        assert env["MODEL"] == "llama-3.1-8b-instruct"


def test_opencode_environment_omits_api_key_when_absent() -> None:
    """Without a key, the process environment does not set VLLM_API_KEY."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            args=["run"],
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert "VLLM_API_KEY" not in env
