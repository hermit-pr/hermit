"""Tests for the opencode integration."""

import json
import tempfile
from pathlib import Path

import pytest

from hermit.opencode import OpenCodeRunner, extract_json_verdict, extract_text


def _run(opencode_opts: dict) -> dict:
    """Generate and read back the opencode config for the given options."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
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
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert "VLLM_API_KEY" not in env


def test_opencode_config_includes_permission_block() -> None:
    """The generated config restricts tools via per-tool permission rules."""
    config = _run({})
    permission = config["permission"]
    assert permission["edit"] == "deny"
    assert permission["webfetch"] == "deny"
    assert permission["question"] == "deny"
    bash = permission["bash"]
    assert bash["*"] == "deny"
    assert bash["git diff*"] == "allow"
    assert bash["git log*"] == "allow"
    assert bash["git show*"] == "allow"
    assert bash["grep*"] == "allow"
    assert bash["cat*"] == "allow"
    assert bash["ls*"] == "allow"
    assert bash["read"] == "allow"


def test_opencode_environment_pins_config_for_antismuggling() -> None:
    """OPENCODE_CONFIG points at the workspace config so repo config is ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert env["OPENCODE_CONFIG"] == str(Path(tmp) / "opencode.json")
        assert env["OPENCODE_CONFIG_DIR"] == tmp


def test_opencode_environment_sets_airgap_flags() -> None:
    """The opencode process gets OPENCODE_DISABLE_* flags for airgap safety."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
        assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
        assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
        assert env["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "1"


def test_opencode_config_restricts_providers_for_airgap() -> None:
    """The generated config only enables the vllm provider."""
    config = _run({})
    assert config["enabled_providers"] == ["vllm"]


def test_opencode_config_disables_autoupdate() -> None:
    """The generated config explicitly disables autoupdate."""
    config = _run({})
    assert config["autoupdate"] is False


def test_opencode_config_disables_sharing() -> None:
    """The generated config explicitly disables session sharing."""
    config = _run({})
    assert config["share"] == "disabled"


def test_extract_text_accumulates_fragments_by_message() -> None:
    """NDJSON text fragments of every message are concatenated in order."""
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
    assert extract_text(stdout) == "Hello worldFinal review"


def test_extract_text_keeps_verdict_from_earlier_message() -> None:
    """A JSON verdict in an earlier message survives later commentary."""
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "messageID": "m1",
                    "part": {
                        "type": "text",
                        "text": '```json\n{"verdict": "Approve"}\n```',
                    },
                }
            ),
            json.dumps(
                {
                    "messageID": "m2",
                    "part": {
                        "type": "text",
                        "text": "I'm ready to produce the final JSON review.",
                    },
                }
            ),
        ]
    )
    markdown = extract_json_verdict(extract_text(stdout))
    assert markdown is not None
    assert "**Approve**" in markdown


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


def test_extract_json_verdict_all_sections() -> None:
    """Valid JSON block with all severity sections is reformatted to markdown."""
    output = (
        "```json\n"
        "{\n"
        '  "critical": [{"title": "crash", "detail": "null deref", '
        '"fix": "check for null"}],\n'
        '  "medium": [{"title": "m1", "detail": "logic error", '
        '"fix": "flip condition"}],\n'
        '  "low": [{"title": "l1", "detail": "style", "fix": "use f-string"}],\n'
        '  "verdict": "Request changes"\n'
        "}\n"
        "```"
    )
    result = extract_json_verdict(output)
    assert result is not None
    assert "## Critical issues" in result
    assert "## Medium issues" in result
    assert "## Low issues" in result
    assert "## Verdict" in result
    assert "**Request changes**" in result
    assert "null deref" in result


def test_extract_json_verdict_empty_sections_omitted() -> None:
    """Sections with empty arrays are not included in the markdown output."""
    output = '```json\n{"critical": [], "medium": [], "verdict": "Approve"}\n```'
    result = extract_json_verdict(output)
    assert result is not None
    assert "## Critical issues" not in result
    assert "## Medium issues" not in result
    assert "## Low issues" not in result
    assert "## Verdict" in result
    assert "**Approve**" in result


def test_extract_json_verdict_missing_verdict() -> None:
    """JSON block without a verdict key returns None."""
    output = '```json\n{"critical": []}\n```'
    result = extract_json_verdict(output)
    assert result is None


def test_extract_json_verdict_malformed() -> None:
    """Invalid JSON (trailing comma) returns None."""
    output = '```json\n{"critical": [],}\n```'
    result = extract_json_verdict(output)
    assert result is None


def test_extract_json_verdict_no_json_block() -> None:
    """Plain text with no fenced JSON block returns None."""
    output = "## Critical issues\n* crash here\n## Verdict\n**Request changes**"
    result = extract_json_verdict(output)
    assert result is None


def test_extract_json_verdict_multiple_blocks_uses_last() -> None:
    """When multiple JSON blocks exist the last one is used."""
    output = (
        '```json\n{"verdict": "Approve"}\n```\n'
        "Some extra text\n"
        '```json\n{"verdict": "Request changes"}\n```'
    )
    result = extract_json_verdict(output)
    assert result is not None
    assert "**Request changes**" in result


def test_extract_json_verdict_inside_noisy_text() -> None:
    """JSON block embedded in preamble text is still found and parsed."""
    output = (
        "Let me think about this diff...\n"
        "Actually, looking at the files...\n"
        '```json\n{"medium": [{"title": "stale ref", "detail": "old name", '
        '"fix": "rename"}], "verdict": "Request changes"}\n```\n'
        "Done."
    )
    result = extract_json_verdict(output)
    assert result is not None
    assert "## Medium issues" in result
    assert "**Request changes**" in result
    assert "Let me think" not in result
    assert "Done" not in result


def test_extract_json_verdict_single_hash_verdict() -> None:
    """JSON verdict with a single critical issue produces correct markdown."""
    output = (
        '```json\n{"critical": [{"title": "XSS", "detail": "unescaped input", '
        '"fix": "escape output"}], "verdict": "Request changes"}\n```'
    )
    result = extract_json_verdict(output)
    assert result is not None
    assert result.startswith("## Critical issues")
    assert "**Request changes**" in result
    assert "XSS" in result
