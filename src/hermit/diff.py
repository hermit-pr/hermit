"""Unified-diff parsing shared by the verdict gate and deterministic checks.

The verdict gate needs more than the text of the changed lines: it must know
which file a line belongs to and where that line sits, so it can resolve a
finding's location itself instead of trusting the model's invented line number.
``iter_changed_lines`` keeps that information; ``changed_lines`` preserves the
flat list of bodies the anchor check has always used.

A diff without hunk headers (a snippet, or the tiny diffs used in tests) is
parsed leniently: every added or removed line is a changed line with an unknown
file and line number.
"""

import re
from dataclasses import dataclass
from typing import Iterator, List

#: Cap on the quoted anchor. Long diff lines (compacted YAML, long shell
#: commands) are legitimate anchors; a low cap silently discarded them.
MAX_ANCHOR_CHARS = 500

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class ChangedLine:
    """One added or removed line, with its file and line number.

    ``line`` is the line number on the side the marker refers to: the old file
    for a ``-`` line, the new file for a ``+`` line. It is ``0`` for a diff
    parsed without hunk headers, where the position is unknown.
    """

    file: str
    line: int
    text: str
    marker: str


def clean_path(raw: str) -> str:
    """Normalize a path taken from a ``---``/``+++`` diff header.

    Git prefixes paths with ``a/`` or ``b/``, may quote them, and appends a tab
    timestamp in some formats. ``/dev/null`` means "this side does not exist",
    so it yields an empty path.
    """
    path = raw.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    if path == "/dev/null":
        return ""
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _hunk_step(
    raw: str, file: str, old_line: int, new_line: int
) -> tuple[ChangedLine | None, int, int, bool]:
    """Consume one line inside a hunk.

    Returns ``(changed_line, old_line, new_line, still_in_hunk)``.
    """
    marker = raw[:1]
    if raw == "" or marker == " ":
        return None, old_line + 1, new_line + 1, True
    if marker == "-":
        return ChangedLine(file, old_line, raw[1:], "-"), old_line + 1, new_line, True
    if marker == "+":
        return ChangedLine(file, new_line, raw[1:], "+"), old_line, new_line + 1, True
    if marker == "\\":
        return None, old_line, new_line, True
    return None, old_line, new_line, False


def iter_changed_lines(diff: str) -> Iterator[ChangedLine]:
    """Yield every added and removed line in *diff*, in file order.

    File headers (``---``/``+++``) and hunk headers are never yielded. A
    ``diff --git`` line starts a new file; a header seen inside a hunk is a
    content line and is handled as such.
    """
    file = ""
    old_line = 0
    new_line = 0
    in_hunk = False
    saw_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            file = ""
            in_hunk = False
            continue
        if not in_hunk and raw.startswith(("--- ", "+++ ")):
            candidate = clean_path(raw[4:])
            if candidate:
                file = candidate
            continue
        match = _HUNK.match(raw)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(2))
            in_hunk = True
            saw_hunk = True
            continue
        if in_hunk:
            line, old_line, new_line, in_hunk = _hunk_step(
                raw, file, old_line, new_line
            )
            if line is not None:
                yield line
            continue
        if not saw_hunk and raw[:1] in ("+", "-"):
            yield ChangedLine(file=file, line=0, text=raw[1:], marker=raw[:1])


def changed_lines(diff: str) -> List[str]:
    """Return the stripped bodies of the added and removed diff lines.

    File headers (``---``/``+++``) are ignored so an anchor cannot match a path
    instead of a changed line.
    """
    return [line.text.strip() for line in iter_changed_lines(diff) if line.text.strip()]


def changed_lines_by_file(diff: str) -> dict[str, List[str]]:
    """Group changed line bodies by the file they belong to."""
    grouped: dict[str, List[str]] = {}
    for line in iter_changed_lines(diff):
        body = line.text.strip()
        if body:
            grouped.setdefault(line.file, []).append(body)
    return grouped


def changed_files(diff: str) -> tuple[str, ...]:
    """Return the files with at least one added or removed line."""
    seen: dict[str, None] = {}
    for line in iter_changed_lines(diff):
        if line.file:
            seen.setdefault(line.file, None)
    return tuple(seen)


def changed_line_count(diff: str) -> int:
    """Return the number of added and removed lines in *diff*."""
    return sum(1 for _ in iter_changed_lines(diff))


def _normalize(text: str) -> str:
    """Collapse whitespace so anchors survive reformatting by the model."""
    return re.sub(r"\s+", " ", text).strip()


def _anchor_candidates(anchor: str) -> List[str]:
    """Return the anchor and, when present, its marker-stripped form.

    An anchor longer than ``MAX_ANCHOR_CHARS`` is compared by its prefix: a
    compacted YAML line or a long ``command:`` can exceed any reasonable cap,
    and rejecting the whole finding over its length silently lost valid ones.
    """
    needle = anchor.strip().strip("`").strip()
    if not needle:
        return []
    needle = needle[:MAX_ANCHOR_CHARS]
    candidates = [needle]
    if needle[:1] in ("+", "-") and needle[1:].strip():
        candidates.append(needle[1:].strip())
    return candidates


def anchor_matches(anchor: str, lines: List[str]) -> bool:
    """Return ``True`` when *anchor* quotes one of the changed *lines*.

    Models often copy a line together with its diff marker (``+``/``-``), so
    both the raw anchor and its marker-stripped form are tried.
    """
    candidates = _anchor_candidates(anchor)
    if not candidates:
        return False
    normalized = [_normalize(candidate) for candidate in candidates]
    for line in lines:
        normalized_line = _normalize(line)
        for candidate, candidate_normalized in zip(candidates, normalized):
            if candidate in line or candidate_normalized in normalized_line:
                return True
    return False


def locate_anchor(anchor: str, diff: str) -> List[ChangedLine]:
    """Return the changed lines *anchor* quotes, exact matches first.

    Substring matches are a fallback: a model may quote a line with different
    surrounding whitespace, or quote the beginning of a long line. When any
    line matches exactly, only those are returned so a short anchor cannot
    resolve to an arbitrary earlier line.
    """
    candidates = _anchor_candidates(anchor)
    if not candidates:
        return []
    normalized = [_normalize(candidate) for candidate in candidates]
    exact: List[ChangedLine] = []
    partial: List[ChangedLine] = []
    for line in iter_changed_lines(diff):
        normalized_line = _normalize(line.text)
        if any(candidate == normalized_line for candidate in normalized):
            exact.append(line)
        elif any(
            candidate in line.text or candidate in normalized_line
            for candidate in normalized
        ):
            partial.append(line)
    return exact or partial
