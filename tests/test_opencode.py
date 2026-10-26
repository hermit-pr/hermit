"""Tests for the opencode integration."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from hermit.config import DEFAULT_REVIEW_RULES
from hermit.opencode import (
    AGENT_NAME,
    MODEL_VARIANT,
    PROVIDER_ID,
    PROVIDER_PACKAGE,
    OpenCodeRunner,
    extract_json_verdict,
    extract_text,
    permission_rules,
    policy_rules,
)


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


def _rules(config: dict, action: str) -> list[dict[str, str]]:
    """Return config permission rules matching ``action``."""
    return [rule for rule in config["permissions"] if rule["action"] == action]


def test_opencode_config_wires_endpoint_and_model() -> None:
    """The generated opencode.json targets the vLLM endpoint and model."""
    config = _run({})
    provider = config["providers"][PROVIDER_ID]
    assert provider["settings"]["baseURL"] == "http://vllm.example:8000/v1"
    assert provider["package"] == PROVIDER_PACKAGE
    assert config["model"] == f"{PROVIDER_ID}/llama-3.1-8b-instruct"
    model = provider["models"]["llama-3.1-8b-instruct"]
    assert model["modelID"] == "llama-3.1-8b-instruct"
    assert model["capabilities"]["tools"] is True


def test_opencode_config_uses_expanded_agent_model_with_variant() -> None:
    """The agent model references the temperature variant (V2 ignores request.body)."""
    config = _run({})
    agent = config["agents"][AGENT_NAME]
    assert agent["model"] == f"{PROVIDER_ID}/llama-3.1-8b-instruct#{MODEL_VARIANT}"
    variants = config["providers"][PROVIDER_ID]["models"]["llama-3.1-8b-instruct"][
        "variants"
    ]
    assert variants[0]["id"] == MODEL_VARIANT
    assert variants[0]["body"]["temperature"] == 0.1


def test_opencode_config_omits_api_key_when_absent() -> None:
    """Without a key, the config does not set an apiKey."""
    config = _run({})
    assert "apiKey" not in config["providers"][PROVIDER_ID]["settings"]


def test_opencode_config_references_key_by_env_when_present() -> None:
    """With a key, the config references it via env var, never in plaintext."""
    config = _run({"api_key": "sk-secret"})
    settings = config["providers"][PROVIDER_ID]["settings"]
    assert settings["apiKey"] == "{env:VLLM_API_KEY}"
    assert "sk-secret" not in json.dumps(settings)


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


def test_opencode_config_denies_everything_by_default() -> None:
    """The first permission rule denies every action; later rules re-allow."""
    config = _run({})
    assert config["permissions"][0] == {
        "action": "*",
        "resource": "*",
        "effect": "deny",
    }


def test_opencode_config_allows_read_only_discovery() -> None:
    """Read, glob and grep stay available for cross-file verification."""
    config = _run({})
    for action in ("read", "glob", "grep"):
        assert _rules(config, action) == [
            {"action": action, "resource": "*", "effect": "allow"}
        ]


def test_opencode_config_shell_allowlist_is_read_only() -> None:
    """Only the documented read-only git commands may run in the shell."""
    config = _run({})
    allowed = {
        rule["resource"]
        for rule in _rules(config, "shell")
        if rule["effect"] == "allow"
    }
    assert "git diff *" in allowed
    assert "git show *" in allowed
    assert "git log *" in allowed
    assert not any("push" in command for command in allowed)
    assert not any("fetch" in command for command in allowed)
    assert not any("xargs" in command for command in allowed)


def test_opencode_config_shell_policies_cannot_be_overridden() -> None:
    """Policies hard-deny shell then lift only the allowlist entries."""
    policies = _run({})["policies"]
    assert {"action": "tool.use", "resource": "shell:*", "effect": "deny"} in policies
    assert {
        "action": "tool.use",
        "resource": "shell:git diff *",
        "effect": "allow",
    } in policies
    assert {
        "action": "tool.use",
        "resource": "edit:*",
        "effect": "deny",
    } in policies
    assert {
        "action": "tool.use",
        "resource": "execute:*",
        "effect": "deny",
    } in policies


def test_opencode_config_restricts_providers_for_airgap() -> None:
    """The generated config only allows the vllm provider, via policies."""
    config = _run({})
    assert config["policies"][0] == {
        "action": "provider.use",
        "resource": "*",
        "effect": "deny",
    }
    assert {
        "action": "provider.use",
        "resource": PROVIDER_ID,
        "effect": "allow",
    } in config["policies"]
    assert "enabled_providers" not in config
    assert "disabled_providers" not in config


def test_opencode_config_denies_integrations() -> None:
    """MCP servers, skills, and plugins from the checkout are never loaded."""
    config = _run({})
    assert {
        "action": "integration.use",
        "resource": "mcp:*",
        "effect": "deny",
    } in config["policies"]
    assert {
        "action": "integration.use",
        "resource": "skill:*",
        "effect": "deny",
    } in config["policies"]
    assert {
        "action": "integration.use",
        "resource": "plugin:*",
        "effect": "deny",
    } in config["policies"]


def test_opencode_config_disables_updates_and_discovery() -> None:
    """Updates, snapshots, and vLLM discovery are all switched off."""
    config = _run({})
    assert config["update"] == "disable"
    assert config["snapshots"] is False
    assert config["plugins"] == ["-opencode.provider.vllm"]
    assert config["tool_output"] == {"max_lines": 400, "max_bytes": 16384}


def test_opencode_config_disables_the_hidden_title_agent() -> None:
    """No extra model call is spent naming a headless session."""
    config = _run({})
    assert config["agents"]["title"] == {"disabled": True}


def test_opencode_config_agent_runs_with_default_rules_and_step_budget() -> None:
    """The reviewer agent carries the operating prompt and a step budget."""
    config = _run({"review_rules": "Be extra strict.", "max_steps": 7})
    agent = config["agents"][AGENT_NAME]
    assert agent["mode"] == "primary"
    assert agent["steps"] == 7
    assert agent["system"].startswith(DEFAULT_REVIEW_RULES)
    assert "Be extra strict." in agent["system"]
    assert agent["permissions"] == config["permissions"]


def test_opencode_config_has_no_legacy_v1_keys() -> None:
    """Nothing from the 1.x schema is emitted any more."""
    config = _run({})
    for legacy in (
        "permission",
        "agent",
        "provider",
        "autoupdate",
        "share",
        "enabled_providers",
        "disabled_providers",
    ):
        assert legacy not in config


def test_opencode_helpers_are_exported_for_inspection() -> None:
    """Rule builders stay pure so they can be asserted directly."""
    assert permission_rules()[0]["effect"] == "deny"
    assert policy_rules()[0]["action"] == "provider.use"


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


def test_opencode_environment_sets_pwd_to_the_checkout() -> None:
    """PWD follows the checkout so opencode resolves the right project."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
            cwd="/workspace/repo",
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert env["PWD"] == "/workspace/repo"


def test_opencode_environment_disables_project_configuration() -> None:
    """A checkout cannot inject its own opencode.json into the reviewer."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = OpenCodeRunner(
            bin_path="opencode",
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=tmp,
        )
        env = runner._environment()  # pylint: disable=protected-access
        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
        assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"


def test_opencode_environment_sets_airgap_flags() -> None:
    """The opencode process gets the airgap flags it still supports."""
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
        assert env["OPENCODE_DISABLE_FREE_MODELS"] == "1"
        assert "OPENCODE_DISABLE_LSP_DOWNLOAD" not in env


def _write_fake_opencode(path: str) -> None:
    """Write an executable stub that reports its argv and working directory."""
    script = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('2.1.4')\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'part': {'type': 'text', 'text': json.dumps("
        "{'cwd': os.getcwd(), 'argv': sys.argv[1:]})}}))\n"
    )
    Path(path).write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)


async def test_opencode_run_spawns_in_repo_dir() -> None:
    """opencode is spawned with cwd pinned to the repository directory."""
    with (
        tempfile.TemporaryDirectory() as workspace,
        tempfile.TemporaryDirectory() as repo,
    ):
        bin_path = os.path.join(workspace, "fake-opencode")
        _write_fake_opencode(bin_path)
        runner = OpenCodeRunner(
            bin_path=bin_path,
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=workspace,
            cwd=repo,
        )
        output = json.loads(await runner.run("review this"))
        assert output["cwd"] == os.path.abspath(repo)
        assert Path(workspace, "opencode.json").exists()
        assert Path(workspace, "review-prompt.md").exists()


async def test_opencode_run_uses_standalone_and_pins_the_agent() -> None:
    """The invocation isolates config and targets the reviewer agent."""
    with (
        tempfile.TemporaryDirectory() as workspace,
        tempfile.TemporaryDirectory() as repo,
    ):
        bin_path = os.path.join(workspace, "fake-opencode")
        _write_fake_opencode(bin_path)
        runner = OpenCodeRunner(
            bin_path=bin_path,
            endpoint="http://vllm.example:8000/v1",
            model="llama-3.1-8b-instruct",
            workspace=workspace,
            cwd=repo,
        )
        argv = json.loads(await runner.run("review this"))["argv"]
        assert argv[:2] == ["--print-logs", "run"]
        assert "--standalone" in argv
        assert argv[argv.index("--agent") + 1] == AGENT_NAME
        assert "--auto" in argv
        assert argv[argv.index("--format") + 1] == "json"
        assert argv[-1] == str(Path(workspace) / "review-prompt.md")


def test_extract_text_accumulates_fragments_by_message() -> None:
    """NDJSON text fragments of every message are concatenated in order."""
    stdout = "\n".join(
        [
            json.dumps({"type": "message", "messageID": "m1"}),
            json.dumps({"part": {"type": "text", "text": "Hello "}}),
            json.dumps({"part": {"type": "text", "text": "world"}}),
            json.dumps({"part": {"type": "tool", "tool": "shell"}}),
            json.dumps({"messageID": "m2", "part": {"type": "text", "text": "Final"}}),
            json.dumps(
                {"messageID": "m2", "part": {"type": "text", "text": " review"}}
            ),
        ]
    )
    assert extract_text(stdout) == "Hello worldFinal review"


def test_extract_text_uses_message_id_nested_in_part() -> None:
    """OpenCode 2.x carries the message id inside the part object."""
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "first", "messageID": "m1"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": " second", "messageID": "m1"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": " third", "messageID": "m2"},
                }
            ),
        ]
    )
    assert extract_text(stdout) == "first second third"


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


def test_extract_text_formats_opencode2_error_payload() -> None:
    """The flat 2.x error payload is surfaced as type and message."""
    stdout = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "provider.transport",
                "message": "getaddrinfo ENOTFOUND vllm.example",
            },
        }
    )
    with pytest.raises(RuntimeError) as excinfo:
        extract_text(stdout)
    assert "provider.transport" in str(excinfo.value)
    assert "ENOTFOUND vllm.example" in str(excinfo.value)


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
