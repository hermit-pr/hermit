"""Tests for the opencode integration."""

import json
import tempfile
from pathlib import Path

import pytest

from hermit.opencode import OpenCodeRunner, extract_text


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


def test_opencode_config_includes_permission_block() -> None:
    """The generated config forbids mutating shell commands."""
    config = _run({})
    permission = config["permission"]
    assert "*git push*" in permission["deny"]
    assert "*git commit*" in permission["deny"]
    assert "*git tag*" in permission["deny"]
    assert "*write*" in permission["deny"]
    assert "git diff*" in permission["allow"]
    assert "git log*" in permission["allow"]
    assert "read" in permission["allow"]


def test_opencode_environment_pins_config_for_antismuggling() -> None:
    """OPENCODE_CONFIG points at the workspace config so repo config is ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            args=["run"],
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert env["OPENCODE_CONFIG"] == str(Path(tmp) / "opencode.json")
        assert env["OPENCODE_CONFIG_DIR"] == tmp


def test_extract_text_accumulates_fragments_by_message() -> None:
    """NDJSON text fragments of the last message are concatenated."""
    stdout = "\n".join(
        [
            json.dumps({"type": "message", "messageID": "m1"}),
            json.dumps({"part": {"type": "text", "text": "Hello "}}),
            json.dumps({"part": {"type": "text", "text": "world"}}),
            json.dumps({"part": {"type": "tool", "tool": "bash"}}),
            json.dumps({"messageID": "m2", "part": {"type": "text", "text": "Final"}}),
            json.dumps(
                {"messageID": "m2", "part": {"type": "text", "text": " review"}}
            ),
        ]
    )
    assert extract_text(stdout) == "Final review"


def test_extract_text_raises_on_error_event() -> None:
    """An error event makes the parser fail loudly."""
    stdout = json.dumps({"type": "error", "error": "model timed out"})
    with pytest.raises(RuntimeError, match="error event"):
        extract_text(stdout)


def test_extract_text_raises_when_no_text() -> None:
    """Output with only metadata produces a clear error."""
    stdout = json.dumps({"type": "message", "messageID": "m1"})
    with pytest.raises(RuntimeError, match="no text output"):
        extract_text(stdout)
