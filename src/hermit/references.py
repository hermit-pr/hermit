"""Deterministic cross-reference scan for names a diff removes or renames.

A compact model cannot be trusted to run the rename ``grep`` itself: in practice
it either skips it, or flags the file the diff already updated, and never the
caller left behind.  The reviewer therefore computes the leftovers itself, from
the diff and the post-change checkout, and quotes them in the prompt as data.
The model only has to turn a verified fact into a finding, with the anchor the
scan hands it.

The scan is deliberately conservative: only declared or imported names are
candidates, short names and names with too many hits are skipped (a real renamed
identifier has a handful of references, a common word has hundreds), comment
lines are ignored, and the policy file is never scanned — a policy that mentions
the old name is not a stale reference.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

#: Names shorter than this are too generic to be a rename (``run``, ``cfg``).
MIN_NAME_LENGTH = 4
#: Cap on the number of names scanned (one ``git grep`` per name).
MAX_CANDIDATE_NAMES = 40
#: Cap on the number of leftovers quoted in the prompt.
MAX_REFERENCES = 40
#: A name with more hits than this is a common word, not a renamed identifier.
MAX_HITS_PER_NAME = 20
#: Cap on the quoted source line.
MAX_REFERENCE_CHARS = 160
#: Timeout of one ``git grep`` invocation.
GREP_TIMEOUT_SECONDS = 60

_DECLARATIONS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)"),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var|function|type|interface)\s+"
        r"([A-Za-z_$]\w*)"
    ),
)
_IMPORT = re.compile(r"^\s*(?:from\s+\S+\s+)?import\s+(.+)$")
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--", ";")
_SCRATCH_PREFIX = ".hermit-"


@dataclass(frozen=True)
class StaleReference:
    """One reference the post-change checkout still makes to a removed name."""

    name: str
    anchor: str
    file: str
    line: int
    text: str


def _imported_names(line: str) -> List[str]:
    """Return the identifiers an ``import`` line binds."""
    match = _IMPORT.match(line)
    if not match:
        return []
    body = match.group(1).split("#", 1)[0]
    names: List[str] = []
    for part in re.split(r"[,()]", body):
        tokens = part.split()
        if not tokens:
            continue
        if "as" in tokens:
            tokens = tokens[tokens.index("as") + 1 :]
        if not tokens:
            continue
        name = tokens[0].split(".")[-1]
        if name and name != "*":
            names.append(name)
    return names


def _declared_names(line: str) -> List[str]:
    """Return the names a line declares or imports."""
    names = [
        match.group(1) for pattern in _DECLARATIONS if (match := pattern.match(line))
    ]
    names.extend(_imported_names(line))
    return names


def removed_names(diff: str) -> Dict[str, str]:
    """Map each name the diff removes to the removed line that declares it.

    A name that reappears in an added line is not removed (that is the new side
    of a rename, or a compatibility alias), so it is filtered out. The returned
    lines are exact removed diff lines, usable verbatim as finding anchors.
    """
    removed: Dict[str, str] = {}
    added: List[str] = []
    for raw in diff.splitlines():
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        marker, body = raw[:1], raw[1:]
        if marker == "+":
            added.append(body)
        elif marker == "-":
            for name in _declared_names(body):
                if len(name) >= MIN_NAME_LENGTH:
                    removed.setdefault(name, body.strip())
    added_text = "\n".join(added)
    return {
        name: anchor
        for name, anchor in removed.items()
        if not re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", added_text)
    }


def _is_comment(text: str) -> bool:
    """Return ``True`` when the hit sits on a comment line."""
    return text.lstrip().startswith(_COMMENT_PREFIXES)


def _is_scratch(file: str, policy_file: str) -> bool:
    """Return ``True`` for hermit-owned or documentation paths."""
    parts = file.replace("\\", "/").split("/")
    if any(part.startswith(_SCRATCH_PREFIX) for part in parts):
        return True
    if not policy_file:
        return False
    return parts[-1] == policy_file.replace("\\", "/").split("/")[-1]


def _grep(repo_dir: str, name: str) -> List[Tuple[str, int, str]]:
    """Return ``(file, line, text)`` hits for *name* in the post-change tree."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "grep", "-n", "-I", "-w", "-F", "--", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=GREP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not scan references for %r: %s", name, exc)
        return []
    if result.returncode not in (0, 1):
        logger.warning("git grep failed for %r: %s", name, result.stderr.strip()[-200:])
        return []
    hits: List[Tuple[str, int, str]] = []
    for raw in result.stdout.splitlines():
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        file, raw_line, text = parts
        try:
            line = int(raw_line)
        except ValueError:
            continue
        hits.append((file, line, text.strip()[:MAX_REFERENCE_CHARS]))
    return hits


def stale_references(
    diff: str,
    repo_dir: str,
    *,
    policy_file: str = "",
    max_references: int = MAX_REFERENCES,
) -> List[StaleReference]:
    """Return the leftovers of every name this diff removed or renamed.

    Args:
        diff: The diff under review.
        repo_dir: The post-change checkout to scan.
        policy_file: The project policy path, never scanned: a policy that
            mentions a renamed symbol is guidance, not a stale reference.
        max_references: Cap on the number of returned references.
    """
    references: List[StaleReference] = []
    candidates = removed_names(diff)
    if len(candidates) > MAX_CANDIDATE_NAMES:
        logger.info(
            "scanning the first %d of %d removed names",
            MAX_CANDIDATE_NAMES,
            len(candidates),
        )
    for name, anchor in list(candidates.items())[:MAX_CANDIDATE_NAMES]:
        hits = _grep(repo_dir, name)
        if not hits:
            continue
        if len(hits) > MAX_HITS_PER_NAME:
            logger.info(
                "skipping %r: %d hits is a common word, not a renamed identifier",
                name,
                len(hits),
            )
            continue
        for file, line, text in hits:
            if _is_comment(text) or _is_scratch(file, policy_file):
                continue
            references.append(
                StaleReference(
                    name=name, anchor=anchor, file=file, line=line, text=text
                )
            )
            if len(references) >= max_references:
                return references
    return references
