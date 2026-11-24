"""Tests for structured verdict parsing and validation."""

import json

import pytest

from hermit.diff import anchor_matches, changed_lines
from hermit.verdict import (
    APPROVE,
    INCONCLUSIVE,
    REQUEST_CHANGES,
    VerdictError,
    inconclusive_review,
    nothing_to_review,
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

TWO_FILE_DIFF = (
    "diff --git a/parse.py b/parse.py\n"
    "--- a/parse.py\n"
    "+++ b/parse.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old_parse()\n"
    "+new_parse()\n"
    "diff --git a/worker.py b/worker.py\n"
    "--- a/worker.py\n"
    "+++ b/worker.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old_worker()\n"
    "+new_worker()\n"
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


def test_parse_verdict_drops_findings_on_hermit_scratch_paths() -> None:
    """A finding naming the extracted policy copy is never published.

    The copy is hermit-owned scratch data holding the base-commit text, so it
    can always be reported as a stale reference; it is dropped even when the
    anchor quotes a real changed line.
    """
    review = parse_verdict(
        _payload(
            medium=[
                _finding(
                    title="policy still mentions parse_config",
                    file="/workspace/.hermit-policy.md",
                )
            ]
        ),
        DIFF,
        ignored_paths=("/workspace/.hermit-policy.md",),
    )
    assert not review.findings
    assert review.dropped == 1
    assert review.verdict == APPROVE


def test_parse_verdict_drops_basename_matches_on_scratch_paths() -> None:
    """A model echoing the bare scratch file name is filtered too."""
    review = parse_verdict(
        _payload(medium=[_finding(file=".hermit-policy.md")]),
        DIFF,
        ignored_paths=("/workspace/.hermit-policy.md",),
    )
    assert not review.findings
    assert review.dropped == 1


def test_parse_verdict_keeps_findings_on_repository_files() -> None:
    """Only hermit-injected paths are filtered, never repository files."""
    review = parse_verdict(
        _payload(medium=[_finding(file="worker.py")]),
        DIFF,
        ignored_paths=("/workspace/.hermit-policy.md",),
    )
    assert len(review.findings) == 1
    assert review.dropped == 0


def test_parse_verdict_marks_request_changes_without_findings_inconclusive() -> None:
    """Ask for changes without a verified finding is inconclusive, not fatal.

    Failing the whole job left the PR with a generic "Review failed" status and
    no review at all; the missing finding is a result the reader must see.
    """
    review = parse_verdict(
        _payload(medium=[_finding(anchor="elsewhere")], verdict="Request changes"),
        DIFF,
    )
    assert review.verdict == INCONCLUSIVE
    assert not review.findings
    assert review.dropped == 1
    markdown = render_markdown(review)
    assert "**Review inconclusive**" in markdown
    assert "this is not an approval" in markdown


def test_parse_verdict_marks_large_empty_approve_inconclusive() -> None:
    """A silent Approve on a substantial diff is not a verified approval."""
    diff = "".join(f"+line {index}\n" for index in range(80))
    review = parse_verdict(_payload(), diff)
    assert review.verdict == INCONCLUSIVE


def test_parse_verdict_keeps_small_empty_approve() -> None:
    """A tiny diff may still be approved without a finding."""
    assert parse_verdict(_payload(), DIFF).verdict == APPROVE


def test_parse_verdict_downgrades_unanchored_finding_in_a_changed_file() -> None:
    """A finding on a changed file survives at low instead of disappearing."""
    review = parse_verdict(
        _payload(
            medium=[_finding(anchor="a line that is not in the diff", file="parse.py")],
            verdict="Request changes",
        ),
        DIFF,
    )
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert finding.severity == "low"
    assert finding.title.startswith("[unanchored]")
    assert review.relaxed == 1
    assert review.dropped == 0
    assert "1 finding(s) downgraded to low" in review.summary
    assert review.verdict == INCONCLUSIVE


def test_parse_verdict_downgraded_model_finding_does_not_hide_a_deterministic_one() -> (
    None
):
    """A low downgraded finding must not suppress the verified medium finding."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-set -euo pipefail\n"
        "+set -u\n"
    )
    review = parse_verdict(
        _payload(
            medium=[
                _finding(
                    title="Missing set -e",
                    file="run.sh",
                    anchor="a line that is not in the diff",
                    detail="errexit is missing",
                )
            ],
            verdict="Request changes",
        ),
        diff,
    )
    titles = [finding.title for finding in review.findings]
    assert "Shell protection removed: errexit (set -e)" in titles
    assert "Shell protection removed: pipefail (set -o pipefail)" in titles
    assert review.verdict == REQUEST_CHANGES


def test_parse_verdict_unknown_severity_stays_advisory() -> None:
    """An unrecognised severity word must not become a blocking finding."""
    payload = {
        "summary": "Renames the parser.",
        "findings": [_finding(severity="suggestion")],
        "verdict": "Approve",
    }
    review = parse_verdict(
        "```json\n" + json.dumps(payload) + "\n```",
        DIFF,
    )
    assert len(review.findings) == 1
    assert review.findings[0].severity == "low"
    assert review.verdict == APPROVE


def test_parse_verdict_accepts_the_canonical_inconclusive_wording() -> None:
    """The documented ``Review inconclusive`` string is a valid verdict."""
    review = parse_verdict(_payload(verdict="Review inconclusive"), DIFF)
    assert review.verdict == INCONCLUSIVE


def test_parse_verdict_downgrades_anchor_from_another_changed_file() -> None:
    """An anchor that belongs to another changed file is misanchored, not trust."""
    review = parse_verdict(
        _payload(
            medium=[_finding(anchor="+new_worker()", file="parse.py")],
            verdict="Request changes",
        ),
        TWO_FILE_DIFF,
    )
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert finding.severity == "low"
    assert finding.title.startswith("[misanchored]")
    assert review.relaxed == 1


def test_parse_verdict_resolves_the_line_from_the_anchor() -> None:
    """A wrong model line number is replaced by the anchor's real position."""
    review = parse_verdict(
        _payload(
            medium=[_finding(file="parse.py", line=99)], verdict="Request changes"
        ),
        DIFF,
    )
    assert review.findings[0].line == 10


def test_parse_verdict_matches_a_long_anchor_by_prefix() -> None:
    """A compacted YAML line longer than the cap is still anchorable."""
    line = "x" * 600
    diff = (
        "diff --git a/big.txt b/big.txt\n"
        "--- a/big.txt\n"
        "+++ b/big.txt\n"
        "@@ -1 +1 @@\n"
        f"+{line}\n"
    )
    review = parse_verdict(
        _payload(
            medium=[_finding(file="big.txt", anchor=line)], verdict="Request changes"
        ),
        diff,
    )
    assert len(review.findings) == 1
    assert review.findings[0].severity == "medium"


def test_parse_verdict_inconclusive_when_only_downgraded() -> None:
    """An unanchored finding is not evidence for approving a substantial diff."""
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n" + "".join(f"+line {index}\n" for index in range(80))
    )
    review = parse_verdict(
        _payload(
            medium=[_finding(file="app.py", anchor="a line that is not there")],
            verdict="Approve",
        ),
        diff,
    )
    assert review.verdict == INCONCLUSIVE
    assert len(review.findings) == 1
    assert review.findings[0].severity == "low"
    assert review.relaxed == 1
    markdown = render_markdown(review)
    assert "did not reach a verified verdict" in markdown


def test_parse_verdict_keeps_findings_when_the_model_says_inconclusive() -> None:
    """An explicit inconclusive verdict keeps the low findings it lists."""
    review = parse_verdict(
        _payload(low=[_finding()], verdict="inconclusive"),
        DIFF,
    )
    assert review.verdict == INCONCLUSIVE
    assert len(review.findings) == 1
    markdown = render_markdown(review)
    assert "did not reach a verified verdict" in markdown
    assert "No finding was verified" not in markdown


def test_parse_verdict_blocking_finding_overrides_an_inconclusive_verdict() -> None:
    """A valid medium finding still requires changes when the model hedges."""
    review = parse_verdict(
        _payload(medium=[_finding()], verdict="inconclusive"),
        DIFF,
    )
    assert review.verdict == REQUEST_CHANGES
    assert len(review.findings) == 1


def test_parse_verdict_reads_a_top_level_findings_array() -> None:
    """Models that use ``findings`` with a severity field are not ignored."""
    payload = {
        "summary": "Renames the parser.",
        "findings": [
            _finding(severity="high", title="caller not updated"),
        ],
        "verdict": "Request changes",
    }
    review = parse_verdict(
        "```json\n" + json.dumps(payload) + "\n```",
        DIFF,
    )
    assert len(review.findings) == 1
    assert review.findings[0].severity == "critical"
    assert review.verdict == REQUEST_CHANGES


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
    """Prose without a JSON verdict is not parseable as a review."""
    with pytest.raises(VerdictError, match="no JSON verdict"):
        parse_verdict("Approve, nothing to report.", DIFF)


def test_inconclusive_review_keeps_the_model_output_tail() -> None:
    """Unparseable prose is published with its evidence, not thrown away."""
    review = inconclusive_review("Approve, nothing to report.", "no JSON verdict")
    assert review.verdict == INCONCLUSIVE
    assert not review.findings
    assert "no JSON verdict" in review.summary
    assert "Approve, nothing to report." in review.summary
    markdown = render_markdown(review)
    assert "**Review inconclusive**" in markdown
    assert "Model output (tail)" in markdown


def test_nothing_to_review_is_a_neutral_inconclusive_result() -> None:
    """An empty diff is a review result, not a bot failure."""
    review = nothing_to_review()
    assert review.verdict == INCONCLUSIVE
    assert not review.findings
    markdown = render_markdown(review)
    assert "Nothing to review" in markdown
    assert "**Review inconclusive**" in markdown


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
