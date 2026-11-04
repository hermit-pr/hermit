"""Tests for deterministic shell-protection loss detection."""

import json

from hermit.protections import lost_protections
from hermit.verdict import REQUEST_CHANGES, parse_verdict, render_markdown

MERGED_SCRIPTS = (
    "diff --git a/old.sh b/old.sh\n"
    "deleted file mode 100755\n"
    "--- a/old.sh\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-#!/bin/bash\n"
    "-set -euo pipefail\n"
    "-echo old\n"
    "diff --git a/new.sh b/new.sh\n"
    "new file mode 100755\n"
    "--- /dev/null\n"
    "+++ b/new.sh\n"
    "@@ -0,0 +1,3 @@\n"
    "+#!/bin/bash\n"
    "+set -uo pipefail\n"
    "+echo new\n"
)

PURE_DELETION = (
    "diff --git a/old.sh b/old.sh\n"
    "deleted file mode 100755\n"
    "--- a/old.sh\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-#!/bin/bash\n"
    "-set -euo pipefail\n"
)


def _payload(**overrides: object) -> str:
    """Return a fenced JSON verdict with the given overrides."""
    base = {
        "summary": "Merges two scripts into one.",
        "verdict": "Approve",
    }
    base.update(overrides)
    return "```json\n" + json.dumps(base) + "\n```"


def test_lost_protection_detects_the_dropped_errexit() -> None:
    """Merging two scripts into one must not silently drop ``set -e``."""
    losses = lost_protections(MERGED_SCRIPTS)
    assert [loss.name for loss in losses] == ["errexit"]
    loss = losses[0]
    assert loss.file == "new.sh"
    assert loss.line == 2
    assert loss.anchor == "set -uo pipefail"
    assert "no added line restores it" in loss.detail


def test_lost_protection_ignores_a_pure_deletion() -> None:
    """Deleting a script is not a regression: nothing was rewritten."""
    assert not lost_protections(PURE_DELETION)


def test_lost_protection_ignores_a_restored_protection() -> None:
    """A protection removed and re-added in the same diff is not lost."""
    diff = MERGED_SCRIPTS.replace("set -uo pipefail", "set -euo pipefail")
    assert not lost_protections(diff)


def test_lost_protection_requires_an_added_set_line() -> None:
    """A removed ``set -e`` with no rewritten script is not reported."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-set -e\n"
        "+echo hello\n"
    )
    assert not lost_protections(diff)


def test_lost_protection_detects_a_removed_exit_trap() -> None:
    """A cleanup trap that disappears with the rewrite is reported."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,3 +1,3 @@\n"
        "-set -euo pipefail\n"
        "-trap cleanup EXIT\n"
        "+set -uo pipefail\n"
        "+cleanup\n"
    )
    losses = lost_protections(diff)
    names = [loss.name for loss in losses]
    assert "trap_exit" in names
    assert "errexit" in names
    assert all(loss.file == "run.sh" for loss in losses)


def test_lost_protection_detects_a_removed_exit_guard() -> None:
    """``cmd || exit`` disappears with the rewrite and no ``set -e`` replaces it."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-openssl verify cert.pem || exit 1\n"
        "+openssl verify cert.pem\n"
        "+set -u\n"
    )
    losses = lost_protections(diff)
    assert [loss.name for loss in losses] == ["exit_guard"]


def test_lost_protection_handles_separate_set_options() -> None:
    """``set -e -o pipefail`` and ``set -euo pipefail`` parse the same way."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-set -e -o pipefail\n"
        "+set -u -o pipefail\n"
    )
    assert [loss.name for loss in lost_protections(diff)] == ["errexit"]


def test_lost_protection_ignores_an_unrelated_set_line() -> None:
    """A deletion plus an unrelated ``set -x`` is not a rewrite."""
    diff = (
        PURE_DELETION + "diff --git a/other.sh b/other.sh\n"
        "--- a/other.sh\n"
        "+++ b/other.sh\n"
        "@@ -1,1 +1,2 @@\n"
        " #!/bin/bash\n"
        "+set -x\n"
    )
    assert not lost_protections(diff)


def test_lost_protection_does_not_blame_an_unrelated_modified_file() -> None:
    """A deleted script is not compared with a modified file elsewhere."""
    diff = (
        PURE_DELETION + "diff --git a/other.sh b/other.sh\n"
        "--- a/other.sh\n"
        "+++ b/other.sh\n"
        "@@ -1,1 +1,2 @@\n"
        " #!/bin/bash\n"
        "+set -u\n"
    )
    assert not lost_protections(diff)


def test_lost_protection_reports_sibling_losses() -> None:
    """Deduplication must not let the first deterministic finding hide the second."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-set -euo pipefail\n"
        "+set -u\n"
    )
    losses = lost_protections(diff)
    assert [loss.name for loss in losses] == ["errexit", "pipefail"]
    review = parse_verdict(_payload(), diff)
    assert len(review.findings) == 2
    assert {finding.title for finding in review.findings} == {
        "Shell protection removed: errexit (set -e)",
        "Shell protection removed: pipefail (set -o pipefail)",
    }


def test_lost_protection_accepts_a_reworded_exit_guard() -> None:
    """``|| { ... exit ...; }`` is the same guard as ``|| exit``."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-openssl verify cert.pem || exit 1\n"
        "+openssl verify cert.pem || { echo failed; exit 1; }\n"
        "+set -u\n"
    )
    assert not lost_protections(diff)


def test_lost_protection_ignores_a_deleted_script_in_another_directory() -> None:
    """A new script is only compared with deleted scripts in its own directory."""
    diff = (
        "diff --git a/other/old.sh b/other/old.sh\n"
        "deleted file mode 100755\n"
        "--- a/other/old.sh\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-#!/bin/bash\n"
        "-set -euo pipefail\n"
        "diff --git a/scripts/new.sh b/scripts/new.sh\n"
        "new file mode 100755\n"
        "--- /dev/null\n"
        "+++ b/scripts/new.sh\n"
        "@@ -0,0 +1,2 @@\n"
        "+#!/bin/bash\n"
        "+set -uo pipefail\n"
    )
    assert not lost_protections(diff)


def test_lost_protection_reports_every_rewritten_file() -> None:
    """Two scripts losing a protection produce two deterministic findings."""
    section = (
        "diff --git a/{name} b/{name}\n"
        "--- a/{name}\n"
        "+++ b/{name}\n"
        "@@ -1,2 +1,2 @@\n"
        "-set -euo pipefail\n"
        "+set -uo pipefail\n"
    )
    diff = section.format(name="one.sh") + section.format(name="two.sh")
    losses = lost_protections(diff)
    assert [loss.file for loss in losses] == ["one.sh", "two.sh"]
    assert [loss.name for loss in losses] == ["errexit", "errexit"]


def test_lost_protection_ignores_comments() -> None:
    """A comment that mentions ``set -e`` is not a declared protection."""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -1,2 +1,2 @@\n"
        "-# set -e used to be here\n"
        "+set -uo pipefail\n"
        "+echo rewritten\n"
    )
    assert not lost_protections(diff)


def test_parse_verdict_injects_a_lost_protection() -> None:
    """A model's empty Approve cannot hide a computed regression by omission."""
    review = parse_verdict(_payload(), MERGED_SCRIPTS)
    assert review.verdict == REQUEST_CHANGES
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert finding.severity == "medium"
    assert "errexit" in finding.title
    assert finding.anchor == "set -uo pipefail"
    assert "1 finding(s) added by hermit's deterministic checks" in review.summary
    assert "errexit" in render_markdown(review)


def test_parse_verdict_does_not_duplicate_a_reported_protection_loss() -> None:
    """When the model already reported it, the deterministic pass stays silent."""
    payload = _payload(
        medium=[
            {
                "title": "The merged script lost set -e",
                "file": "new.sh",
                "line": 2,
                "anchor": "set -uo pipefail",
                "impact": "a failing command no longer aborts the script",
                "detail": "errexit is missing",
                "fix": "add set -e",
            }
        ],
        verdict="Request changes",
    )
    review = parse_verdict(payload, MERGED_SCRIPTS)
    assert len(review.findings) == 1
    assert review.findings[0].title == "The merged script lost set -e"
