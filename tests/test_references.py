"""Tests for the deterministic cross-reference scan."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hermit.references import removed_names, stale_references

RENAME_DIFF = (
    "diff --git a/config.py b/config.py\n"
    "--- a/config.py\n"
    "+++ b/config.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def parse_config(path):\n"
    "+def load_config(path):\n"
    "     return path\n"
)


def test_removed_names_maps_a_renamed_definition_to_its_removed_line() -> None:
    """The anchor is the exact removed diff line, ready for a finding."""
    assert removed_names(RENAME_DIFF) == {"parse_config": "def parse_config(path):"}


def test_removed_names_reads_imports_classes_and_js_declarations() -> None:
    """Every declaration shape the reviewer cares about is recognised."""
    diff = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1,4 +1,4 @@\n"
        "-from config import parse_config, loader\n"
        "-class RepoCache:\n"
        "-const fetchAll = () => 1;\n"
        "+from config import load_config, loader\n"
        "+class CacheStore:\n"
        "+const fetchEverything = () => 1;\n"
    )
    assert removed_names(diff) == {
        "parse_config": "from config import parse_config, loader",
        "RepoCache": "class RepoCache:",
        "fetchAll": "const fetchAll = () => 1;",
    }


def test_removed_names_ignores_names_reintroduced_by_the_diff() -> None:
    """A name that an added line restores is not stale anywhere."""
    diff = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-from config import parse_config\n"
        "+from config import parse_config  # kept for compatibility\n"
        "+parse_config = load_config\n"
    )
    assert removed_names(diff) == {}


def test_removed_names_skips_short_and_undeclared_names() -> None:
    """Generic identifiers are left to the model instead of flooding the scan."""
    diff = (
        "--- a/worker.py\n"
        "+++ b/worker.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-def run():\n"
        "+def start():\n"
        "-    total = compute()\n"
        "+    subtotal = compute()\n"
        "     return 0\n"
    )
    assert removed_names(diff) == {}


def _git(repo: Path, *args: str) -> str:
    """Run a git command in *repo* and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _build_rename_repo(root: Path) -> tuple[str, Path]:
    """Create a checkout where a rename updated one caller but not another."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "config.py").write_text(
        "def parse_config(path):\n    return path\n", encoding="utf-8"
    )
    (repo / "main.py").write_text("from config import parse_config\n", encoding="utf-8")
    (repo / "worker.py").write_text(
        "# parse_config is used here too\n"
        "from config import parse_config\n"
        "parse_config('worker.json')\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "- load configuration through `parse_config`\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "tag", "target-branch")
    (repo / "config.py").write_text(
        "def load_config(path):\n    return path\n", encoding="utf-8"
    )
    (repo / "main.py").write_text("from config import load_config\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rename")
    diff = _git(repo, "diff", "target-branch")
    return diff, repo


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_stale_references_finds_the_unchanged_caller() -> None:
    """The caller the diff forgot is reported with a usable anchor."""
    with tempfile.TemporaryDirectory() as tmp:
        diff, repo = _build_rename_repo(Path(tmp))
        references = stale_references(diff, str(repo), policy_file="AGENTS.md")
    assert [(ref.name, ref.file) for ref in references] == [
        ("parse_config", "worker.py"),
        ("parse_config", "worker.py"),
    ]
    assert all(ref.anchor == "def parse_config(path):" for ref in references)
    assert references[0].line == 2
    assert "from config import parse_config" in references[0].text


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_stale_references_skips_comments_and_the_policy_file() -> None:
    """Comment mentions and the policy document are not stale references."""
    with tempfile.TemporaryDirectory() as tmp:
        diff, repo = _build_rename_repo(Path(tmp))
        references = stale_references(diff, str(repo), policy_file="AGENTS.md")
    assert all(not ref.text.lstrip().startswith("#") for ref in references)
    assert all(ref.file != "AGENTS.md" for ref in references)


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)
def test_stale_references_without_policy_file() -> None:
    """Without a policy path, the policy document is scanned like any file."""
    with tempfile.TemporaryDirectory() as tmp:
        diff, repo = _build_rename_repo(Path(tmp))
        references = stale_references(diff, str(repo))
    assert any(ref.file == "AGENTS.md" for ref in references)


def test_stale_references_skips_hermit_scratch_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit inside a hermit-owned file is never a repository reference."""

    def fake_grep(_repo_dir: str, _name: str) -> list[tuple[str, int, str]]:
        """Return one real hit and one inside hermit's policy copy."""
        return [
            ("worker.py", 2, "from config import parse_config"),
            (".hermit-policy.md", 4, "use parse_config"),
        ]

    monkeypatch.setattr("hermit.references._grep", fake_grep)
    references = stale_references(RENAME_DIFF, "/nowhere", policy_file="AGENTS.md")
    assert [ref.file for ref in references] == ["worker.py"]


def test_stale_references_skips_common_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name with too many hits is a common word, not a renamed identifier."""
    many = [("file.py", index, "name") for index in range(21)]

    def fake_grep(_repo_dir: str, _name: str) -> list[tuple[str, int, str]]:
        """Return more hits than the noise threshold."""
        return many

    monkeypatch.setattr("hermit.references._grep", fake_grep)
    assert not stale_references(RENAME_DIFF, "/nowhere")


def test_stale_references_caps_the_number_of_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected block is bounded so a rename cannot flood the prompt."""

    def fake_grep(_repo_dir: str, _name: str) -> list[tuple[str, int, str]]:
        """Return more hits than the cap."""
        return [(f"file{index}.py", index, "parse_config(...)") for index in range(5)]

    monkeypatch.setattr("hermit.references._grep", fake_grep)
    references = stale_references(RENAME_DIFF, "/nowhere", max_references=2)
    assert len(references) == 2


def test_stale_references_survives_an_unusable_checkout() -> None:
    """A missing checkout degrades to no reference instead of failing the job."""
    assert not stale_references(RENAME_DIFF, "/does/not/exist")
