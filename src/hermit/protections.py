"""Deterministic detection of shell protections a change drops.

A regression by omission has no removed line of its own to anchor a finding on.
When two scripts that both declared ``set -euo pipefail`` are merged into one
that declares only ``set -uo pipefail``, the missing ``set -e`` is in the diff
only as an absence, so a diff-scoped reviewer cannot report it and the anchor
gate would discard a finding that tried.

Hermit therefore derives these losses itself, exactly as ``hermit.references``
does for stale names: the protections declared by removed lines are compared
with those declared by added lines, and every protection that is not restored
becomes a deterministic ``medium`` finding anchored on the added ``set -`` line
that now configures the script. The check never depends on the model: same
diff, same result, no temperature.

The comparison is intentionally conservative. It only fires when the change
adds a ``set -`` line (i.e. it rewrites a script's shell options), ignores
comment lines, does not report a protection that an added line restores, and
does not treat an ``|| exit`` guard as lost when ``set -e`` was added instead.
"""

import posixpath
import re
from dataclasses import dataclass
from typing import Dict, List

from hermit.diff import ChangedLine, clean_path, iter_changed_lines

_SHELL_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh", ".dash")
_SHEBANG = re.compile(r"^#!\s*(?:\S*/)?(?:env\s+)?(?:ba|z|k|da)?sh(?:\s|$)")
_SET_LINE = re.compile(r"^\s*set\s+(?P<flags>[+-][A-Za-z]+)(?P<rest>.*)$")
_TRAP_LINE = re.compile(r"^\s*trap\s+")
_EXIT_GUARD = re.compile(r"\|\|\s*(?:\{[^}]*\b(?:exit|return)\b|(?:exit|return)\b)")
_COMMENT_PREFIXES = ("#", "//")

#: Stable order in which multiple losses are reported.
_PROTECTION_ORDER = (
    "errexit",
    "nounset",
    "pipefail",
    "trap_exit",
    "trap_err",
    "exit_guard",
)

_TEXT: Dict[str, tuple[str, tuple[str, ...], str, str, str]] = {
    "errexit": (
        "errexit (set -e)",
        ("set -e", "errexit"),
        "A failing command no longer aborts the script, so it can continue and "
        "report success after a partial failure.",
        "Restore `set -e` (or check every command's exit status explicitly).",
        "the new script must stop on the first failed command",
    ),
    "nounset": (
        "nounset (set -u)",
        ("set -u", "nounset"),
        "An unset variable no longer stops the script; it can expand to an "
        "empty value and corrupt a path or a decision.",
        "Restore `set -u` (or initialise the variable before use).",
        "the new script must fail on an unset variable",
    ),
    "pipefail": (
        "pipefail (set -o pipefail)",
        ("pipefail",),
        "A failure in an earlier stage of a pipeline is hidden by the last "
        "stage, so a broken pipeline can look successful.",
        "Restore `set -o pipefail` (or check every stage's status explicitly).",
        "the new script must report a failed pipeline stage",
    ),
    "trap_exit": (
        "EXIT trap",
        ("trap", "EXIT"),
        "Cleanup or the final status report no longer runs when the script exits.",
        "Restore the `trap ... EXIT` handler.",
        "cleanup must still run on every exit path",
    ),
    "trap_err": (
        "ERR trap",
        ("trap", "ERR"),
        "Errors are no longer handled by the trap and can pass silently.",
        "Restore the `trap ... ERR` handler.",
        "errors must still reach the trap",
    ),
    "exit_guard": (
        "|| exit guard",
        ("|| exit",),
        "A failing command is no longer followed by `|| exit`, so the script "
        "can continue after it.",
        "Restore the `|| exit` guard (or enable `set -e`).",
        "a failed command must stop the script",
    ),
}


@dataclass(frozen=True)
class LostProtection:
    """A shell protection a diff removed without restoring it."""

    name: str
    title: str
    detail: str
    impact: str
    fix: str
    file: str
    line: int
    anchor: str
    needles: tuple[str, ...]


def _is_comment(text: str) -> bool:
    """Return ``True`` when *text* is a comment line, not shell code."""
    return text.lstrip().startswith(_COMMENT_PREFIXES)


def _set_protections(line: str) -> set[str]:
    """Return the protections a ``set`` command on *line* declares."""
    found: set[str] = set()
    tokens = line.split()
    if not tokens or tokens[0] != "set":
        return found
    option_mode = False
    for token in tokens[1:]:
        if token == "--":
            break
        if token.startswith("+"):
            option_mode = False
            continue
        if token.startswith("-"):
            flags = token[1:]
            if "e" in flags:
                found.add("errexit")
            if "u" in flags:
                found.add("nounset")
            option_mode = "o" in flags
            continue
        if option_mode and token in ("errexit", "nounset", "pipefail"):
            found.add(token)
        option_mode = False
    return found


def _protections(line: str) -> set[str]:
    """Return the shell protections *line* declares."""
    if _is_comment(line):
        return set()
    found = _set_protections(line)
    if _TRAP_LINE.match(line):
        if re.search(r"\bEXIT\b", line):
            found.add("trap_exit")
        if re.search(r"\bERR\b", line):
            found.add("trap_err")
    if _EXIT_GUARD.search(line):
        found.add("exit_guard")
    return found


def _is_shell_file(file: str, changes: List[ChangedLine]) -> bool:
    """Return ``True`` when *file* looks like a shell script."""
    if file.lower().endswith(_SHELL_SUFFIXES):
        return True
    return any(_SHEBANG.match(change.text) for change in changes)


def _added_set_line(changes: List[ChangedLine]) -> ChangedLine | None:
    """Return the first added ``set -`` line in *changes*, if any."""
    for change in changes:
        if change.marker == "+" and _SET_LINE.match(change.text):
            return change
    return None


def _protections_from(changes: List[ChangedLine], marker: str) -> set[str]:
    """Return the protections declared by lines with *marker*."""
    found: set[str] = set()
    for change in changes:
        if change.marker == marker:
            found |= _protections(change.text)
    return found


def _store_status(
    statuses: Dict[str, str],
    current: str,
    *,
    new_file: bool,
    deleted: bool,
    renamed: bool,
) -> None:
    """Record the status of the file just parsed."""
    if not current:
        return
    if deleted:
        statuses[current] = "deleted"
    elif new_file:
        statuses[current] = "new"
    elif renamed:
        statuses[current] = "renamed"
    else:
        statuses[current] = "modified"


def _status_step(
    line: str,
    current: str,
    *,
    new_file: bool,
    deleted: bool,
    renamed: bool,
    in_hunk: bool,
) -> tuple[str, bool, bool, bool, bool]:
    """Update the file-status state for one line that is not ``diff --git``."""
    result = (current, new_file, deleted, renamed, in_hunk)
    if line.startswith("@@ "):
        result = (current, new_file, deleted, renamed, True)
    elif in_hunk:
        result = (current, new_file, deleted, renamed, True)
    elif line.startswith("new file mode"):
        result = (current, True, deleted, renamed, False)
    elif line.startswith("deleted file mode"):
        result = (current, new_file, True, renamed, False)
    elif line.startswith("rename from "):
        result = (current, new_file, deleted, True, False)
    elif line.startswith("--- "):
        raw = line[4:].strip()
        if raw == "/dev/null":
            result = (current, True, deleted, renamed, False)
        else:
            result = (current or clean_path(raw), new_file, deleted, renamed, False)
    elif line.startswith("+++ "):
        raw = line[4:].strip()
        if raw == "/dev/null":
            result = (current, new_file, True, renamed, False)
        else:
            result = (clean_path(raw), new_file, deleted, renamed, False)
    return result


def _file_statuses(diff: str) -> Dict[str, str]:
    """Map each changed file to ``new``, ``deleted``, ``renamed`` or ``modified``.

    A merge usually lands as one new script plus the deleted scripts it
    replaces, so the distinction is what lets the protection check compare the
    right files instead of unioning the whole diff.
    """
    statuses: Dict[str, str] = {}
    current = ""
    new_file = False
    deleted = False
    renamed = False
    in_hunk = False

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            _store_status(
                statuses,
                current,
                new_file=new_file,
                deleted=deleted,
                renamed=renamed,
            )
            current = ""
            new_file = False
            deleted = False
            renamed = False
            in_hunk = False
            continue
        current, new_file, deleted, renamed, in_hunk = _status_step(
            line,
            current,
            new_file=new_file,
            deleted=deleted,
            renamed=renamed,
            in_hunk=in_hunk,
        )
    _store_status(
        statuses, current, new_file=new_file, deleted=deleted, renamed=renamed
    )
    return statuses


def _removed_example(sources: List[List[ChangedLine]], name: str) -> str:
    """Return one removed line that declared *name*, trimmed for display."""
    for changes in sources:
        for change in changes:
            if change.marker == "-" and name in _protections(change.text):
                return change.text.strip()[:160]
    return ""


def _losses_for_anchor(
    anchor: ChangedLine,
    by_file: Dict[str, List[ChangedLine]],
    statuses: Dict[str, str],
) -> tuple[set[str], List[List[ChangedLine]]]:
    """Compare *anchor*'s file with the files it can actually replace.

    A modified or renamed file only loses protections it removed itself; a new
    file may be a merge of deleted scripts in the same directory, so those are
    included. An unrelated ``set -`` addition in a modified file can therefore
    never blame a deletion.
    """
    changes = by_file.get(anchor.file, [])
    added = _protections_from(changes, "+")
    if not added:
        return set(), []
    sources = [changes]
    if statuses.get(anchor.file) == "new":
        anchor_dir = posixpath.dirname(anchor.file)
        sources.extend(
            deleted
            for file, deleted in by_file.items()
            if file != anchor.file
            and statuses.get(file) == "deleted"
            and posixpath.dirname(file) == anchor_dir
        )
    removed: set[str] = set()
    for source in sources:
        removed |= _protections_from(source, "-")
    return _lost_names(removed, added), sources


def _candidate_anchors(
    by_file: Dict[str, List[ChangedLine]], statuses: Dict[str, str]
) -> List[ChangedLine]:
    """Return added ``set -`` lines, preferring new shell files."""
    candidates: List[ChangedLine] = []
    for changes in by_file.values():
        anchor = _added_set_line(changes)
        if anchor is not None:
            candidates.append(anchor)
    candidates.sort(
        key=lambda anchor: (
            not _is_shell_file(anchor.file, by_file[anchor.file]),
            statuses.get(anchor.file) != "new",
        )
    )
    return candidates


def _build(name: str, anchor: ChangedLine, removed_line: str) -> LostProtection:
    """Build the deterministic finding for one lost protection."""
    label, needles, impact, fix, intent = _TEXT[name]
    if removed_line:
        detail = f"The diff removes `{removed_line}` but no added line restores it. "
    else:
        detail = (
            f"The diff removes the {label} protection but no added line restores it. "
        )
    detail += (
        f"The rewritten script declares `{anchor.text.strip()}`, which no longer "
        f"carries it; {intent}."
    )
    return LostProtection(
        name=name,
        title=f"Shell protection removed: {label}",
        detail=detail,
        impact=impact,
        fix=fix,
        file=anchor.file,
        line=anchor.line,
        anchor=anchor.text.strip(),
        needles=needles,
    )


def _lost_names(removed: set[str], added: set[str]) -> set[str]:
    """Return protections that were removed and not restored."""
    lost = removed - added
    # ``set -e`` already stops the script on a failed command, so the loss of
    # an explicit ``|| exit`` is not a regression in that case.
    if "exit_guard" in lost and "errexit" in added:
        lost.discard("exit_guard")
    return lost


def lost_protections(diff: str) -> List[LostProtection]:
    """Return the shell protections this diff drops without restoring.

    Each added ``set -`` line is considered as the rewritten script. A modified
    or renamed file is compared with its own removed lines; a new file is also
    compared with the deleted scripts it replaces (the merge/rename shape). The
    check only fires when an added line restores at least one protection, so a
    pure deletion or an unrelated ``set -x`` is never reported.
    """
    by_file: Dict[str, List[ChangedLine]] = {}
    for change in iter_changed_lines(diff):
        by_file.setdefault(change.file, []).append(change)
    if not by_file:
        return []

    statuses = _file_statuses(diff)
    results: List[LostProtection] = []
    seen: set[tuple[str, int, str]] = set()
    for anchor in _candidate_anchors(by_file, statuses):
        lost, sources = _losses_for_anchor(anchor, by_file, statuses)
        for name in _PROTECTION_ORDER:
            if name not in lost:
                continue
            key = (anchor.file, anchor.line, name)
            if key in seen:
                continue
            seen.add(key)
            results.append(_build(name, anchor, _removed_example(sources, name)))
    return results
