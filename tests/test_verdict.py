"""Tests for structured verdict parsing and validation."""

import json

import pytest

from hermit.verdict import (
    APPROVE,
    REQUEST_CHANGES,
    VerdictError,
    anchor_matches,
    changed_lines,
    parse_verdict,
    render_markdown,
)

DIFF = (
    "diff --git a/parse.py b/parse.py\n"
    "--- a/parse.py\n"
    "+++ b/parse.py\n"
    "@@ -10,7 +10,7 @@\n"
    "-def parse_config(path):\n"
    "+def load_config(path):\n"
    "     with open(path) as handle:\n"
    "-        return json.load(handle)\n"
    "+        return json.loads(handle.read())\n"
)


def _payload(**overrides: object) -> str:
    """Return a fenced JSON verdict with the given overrides."""
    base = {
        "summary": "Renames parse_config to load_config.",
        "verdict": "Approve",
    }
    base.update(overrides)
    return "```json\n" + json.dumps(base) + "\n```"


def _finding(**overrides: object) -> dict:
    """Return a finding anchored on the renamed function."""
    base = {
        "title": "caller not updated",
        "file": "main.py",
        "line": 4,
        "anchor": "def load_config(path):",
        "impact": "callers still import parse_config",
        "detail": "the symbol was renamed",
        "fix": "update the caller",
    }
    base.update(overrides)
    return base


def test_changed_lines_ignores_file_headers() -> None:
    """Only added/removed bodies are considered, never the ---/+++ headers."""
    lines = changed_lines(DIFF)
    assert "def load_config(path):" in lines
    assert "def parse_config(path):" in lines
    assert "b/parse.py" not in lines


def test_anchor_matches_tolerates_whitespace() -> None:
    """A reformatted quote still matches the changed line."""
    lines = changed_lines(DIFF)
    assert anchor_matches("def    load_config(path):", lines)
    assert not anchor_matches("def other_function(path):", lines)
    assert not anchor_matches("", lines)


def test_anchor_matches_tolerates_diff_markers() -> None:
    """A line copied with its + or - diff marker still matches."""
    lines = changed_lines(DIFF)
    assert anchor_matches("-def parse_config(path):", lines)
    assert anchor_matches("+def load_config(path):", lines)
    assert not anchor_matches("-def dropped_function(path):", lines)


def test_anchor_rejects_file_headers() -> None:
    """An anchor quoting a path header is out of scope."""
    assert not anchor_matches("b/parse.py", changed_lines(DIFF))


def test_parse_verdict_approve_with_summary() -> None:
    """A clean approve only needs a summary."""
    review = parse_verdict(_payload(), DIFF)
    assert review.verdict == APPROVE
    assert not review.findings
    assert review.dropped == 0
    assert "Renames parse_config" in render_markdown(review)


def test_parse_verdict_keeps_anchored_findings() -> None:
    """A finding quoting a changed line survives validation."""
    review = parse_verdict(
        _payload(medium=[_finding()], verdict="Request changes"), DIFF
    )
    assert review.verdict == REQUEST_CHANGES
    assert len(review.findings) == 1
    markdown = render_markdown(review)
    assert "## Medium findings" in markdown
    assert "`main.py:4`" in markdown
    assert "**Impact**: callers still import parse_config" in markdown
    assert "**Fix**: update the caller" in markdown


def test_parse_verdict_drops_unanchored_findings() -> None:
    """A finding about untouched code is dropped instead of published."""
    review = parse_verdict(
        _payload(
            low=[
                _finding(
                    title="pre-existing issue",
                    anchor="totally unrelated line",
                )
            ]
        ),
        DIFF,
    )
    assert not review.findings
    assert review.verdict == APPROVE
    assert review.dropped == 1


def test_parse_verdict_drops_non_object_findings() -> None:
    """Malformed finding entries are dropped and counted."""
    review = parse_verdict(_payload(medium=["not an object"]), DIFF)
    assert not review.findings
    assert review.dropped == 1


def test_parse_verdict_rejects_request_changes_without_findings() -> None:
    """Ask for changes without an anchored finding is a failed review."""
    with pytest.raises(VerdictError, match="no finding is anchored"):
        parse_verdict(
            _payload(medium=[_finding(anchor="elsewhere")], verdict="Request changes"),
            DIFF,
        )


def test_parse_verdict_forces_request_changes_for_blocking_findings() -> None:
    """An Approve verdict cannot hide a critical or medium finding."""
    review = parse_verdict(_payload(medium=[_finding()]), DIFF)
    assert review.verdict == REQUEST_CHANGES


def test_parse_verdict_requires_summary() -> None:
    """The summary is the proof the model read the diff."""
    with pytest.raises(VerdictError, match="summary"):
        parse_verdict(_payload(summary="   "), DIFF)


def test_parse_verdict_requires_verdict_field() -> None:
    """A JSON object without a verdict is not a review."""
    with pytest.raises(VerdictError, match="verdict"):
        parse_verdict('```json\n{"summary": "x"}\n```', DIFF)


def test_parse_verdict_rejects_unknown_verdict() -> None:
    """Unknown verdict wording is rejected instead of guessed."""
    with pytest.raises(VerdictError, match="unrecognised verdict"):
        parse_verdict(_payload(verdict="Maybe"), DIFF)


def test_parse_verdict_accepts_raw_json_without_fence() -> None:
    """Small models often forget the code fence."""
    raw = json.dumps({"summary": "Touches parse.py only.", "verdict": "approve"})
    assert parse_verdict(raw, DIFF).verdict == APPROVE


def test_parse_verdict_uses_the_last_json_block() -> None:
    """When the model revises its answer, the last block wins."""
    output = (
        '```json\n{"summary": "first", "verdict": "Approve"}\n```\n'
        '```json\n{"summary": "second", "verdict": "Approve"}\n```'
    )
    assert parse_verdict(output, DIFF).summary == "second"


def test_parse_verdict_rejects_malformed_json() -> None:
    """Invalid JSON fails instead of falling back to free-form text."""
    with pytest.raises(VerdictError, match="invalid JSON"):
        parse_verdict('```json\n{"verdict": "Approve",}\n```', DIFF)


def test_parse_verdict_rejects_missing_json() -> None:
    """Prose without a JSON verdict is a failed review."""
    with pytest.raises(VerdictError, match="no JSON verdict"):
        parse_verdict("Approve, nothing to report.", DIFF)


def test_render_markdown_orders_sections() -> None:
    """Summary comes first, then severities, then the verdict."""
    review = parse_verdict(
        _payload(
            critical=[_finding(file="a.py")],
            medium=[_finding()],
            verdict="Request changes",
        ),
        DIFF,
    )
    markdown = render_markdown(review)
    assert markdown.index("## Summary") < markdown.index("## Critical findings")
    assert markdown.index("## Critical findings") < markdown.index("## Medium findings")
    assert markdown.index("## Medium findings") < markdown.index("## Verdict")
