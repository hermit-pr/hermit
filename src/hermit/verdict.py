"""Structured review verdict: parsing, validation, and rendering.

The reviewer is asked for one JSON block with a mandatory ``summary`` and one
``anchor`` per finding: an exact quote of a line the diff added or removed. That
gives the bot a deterministic check on scope, independent of the model's
judgement — a finding whose anchor is not part of the diff is dropped, so a
model that wanders into unrelated code cannot turn that into review noise.
"""

import json
import logging
import posixpath
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SEVERITIES = ("critical", "medium", "low")
SEVERITY_TITLES = {"critical": "Critical", "medium": "Medium", "low": "Low"}
BLOCKING_SEVERITIES = ("critical", "medium")

APPROVE = "Approve"
REQUEST_CHANGES = "Request changes"
VALID_VERDICTS = (APPROVE, REQUEST_CHANGES)

MAX_ANCHOR_CHARS = 200

_JSON_FENCE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
_ANY_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)\s*```", re.DOTALL)


class VerdictError(ValueError):
    """Raised when model output cannot be turned into a valid review."""


@dataclass(frozen=True)
class Finding:
    """One validated review finding."""

    severity: str
    title: str
    detail: str
    fix: str
    impact: str
    file: str
    line: int
    anchor: str


@dataclass(frozen=True)
class Review:
    """A validated review ready to be rendered as markdown."""

    summary: str
    findings: tuple[Finding, ...]
    verdict: str
    dropped: int = 0


def changed_lines(diff: str) -> list[str]:
    """Return the stripped bodies of the added and removed diff lines.

    File headers (``---``/``+++``) are ignored so an anchor cannot match a path
    instead of a changed line.
    """
    lines: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw[:1] not in ("+", "-"):
            continue
        body = raw[1:].strip()
        if body:
            lines.append(body)
    return lines


def _normalize(text: str) -> str:
    """Collapse whitespace so anchors survive reformatting by the model."""
    return re.sub(r"\s+", " ", text).strip()


def anchor_matches(anchor: str, lines: list[str]) -> bool:
    """Return ``True`` when *anchor* quotes one of the changed *lines*.

    Models often copy a line together with its diff marker (``+``/``-``), so
    both the raw anchor and its marker-stripped form are tried.
    """
    needle = anchor.strip().strip("`").strip()
    if not needle or len(needle) > MAX_ANCHOR_CHARS:
        return False
    candidates = [needle]
    if needle[:1] in ("+", "-") and needle[1:].strip():
        candidates.append(needle[1:].strip())
    normalized = [_normalize(candidate) for candidate in candidates]
    for line in lines:
        normalized_line = _normalize(line)
        for candidate, candidate_normalized in zip(candidates, normalized):
            if candidate in line or candidate_normalized in normalized_line:
                return True
    return False


def _json_payload(output: str) -> dict:
    """Extract the review JSON object from the model output."""
    candidates = _JSON_FENCE.findall(output)
    if not candidates:
        candidates = _ANY_FENCE.findall(output)
    if not candidates:
        start, end = output.find("{"), output.rfind("}")
        if start == -1 or end <= start:
            raise VerdictError("opencode produced no JSON verdict")
        candidates = [output[start : end + 1]]
    try:
        payload = json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise VerdictError(f"invalid JSON verdict: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerdictError("JSON verdict is not an object")
    return payload


def _normalize_verdict(value: object) -> str:
    """Map the model's verdict text onto the two supported verdicts."""
    text = str(value).strip().lower()
    if text.startswith("approve") or text in {"lgtm", "ok", "pass"}:
        return APPROVE
    if "request" in text or text.startswith("change"):
        return REQUEST_CHANGES
    raise VerdictError(f"unrecognised verdict {value!r}")


def _matches_ignored(file: str, ignored_paths: tuple[str, ...]) -> bool:
    """Return ``True`` when *file* names a path hermit injected itself.

    The extracted policy copy is hermit-owned scratch, never repository
    content, so a finding may not target it. The comparison is on the file name:
    the model may echo the absolute path, a checkout-relative path, or the bare
    name, and ``.hermit-policy.md`` is a name hermit reserves.
    """
    cleaned = file.strip().strip("`").strip()
    if not cleaned:
        return False
    name = posixpath.basename(posixpath.normpath(cleaned.replace("\\", "/")))
    return any(
        name == posixpath.basename(posixpath.normpath(ignored.strip()))
        for ignored in ignored_paths
    )


def _parse_findings(
    payload: dict, lines: list[str], ignored_paths: tuple[str, ...] = ()
) -> tuple[tuple[Finding, ...], int]:
    """Build findings, dropping any whose anchor is not in the diff."""
    findings: list[Finding] = []
    dropped = 0
    for severity in SEVERITIES:
        entries = payload.get(severity, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            anchor = str(entry.get("anchor") or "")
            title = str(entry.get("title") or "Untitled")
            file = str(entry.get("file") or "")
            if _matches_ignored(file, ignored_paths):
                dropped += 1
                logger.warning(
                    "dropping %s finding on hermit scratch path %r", severity, file
                )
                continue
            if not anchor_matches(anchor, lines):
                dropped += 1
                logger.warning(
                    "dropping %s finding outside the diff: %s", severity, title
                )
                continue
            line = entry.get("line", 0)
            findings.append(
                Finding(
                    severity=severity,
                    title=title,
                    detail=str(entry.get("detail") or ""),
                    fix=str(entry.get("fix") or ""),
                    impact=str(entry.get("impact") or ""),
                    file=file,
                    line=line if isinstance(line, int) else 0,
                    anchor=anchor.strip(),
                )
            )
    return tuple(findings), dropped


def parse_verdict(
    output: str, diff: str, *, ignored_paths: tuple[str, ...] = ()
) -> Review:
    """Parse and validate the model output against the diff it reviewed.

    Args:
        output: The text produced by opencode.
        diff: The diff handed to the model, used to verify every anchor.
        ignored_paths: Paths hermit wrote itself (the extracted policy copy).
            A finding naming one of them is discarded: it would report
            scratch data as repository content.

    Raises:
        VerdictError: if there is no JSON, no summary, no valid verdict, or the
            model asks for changes without any finding anchored in the diff.
    """
    payload = _json_payload(output)
    if "verdict" not in payload:
        raise VerdictError("JSON verdict has no `verdict` field")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise VerdictError("JSON verdict has no `summary`")
    verdict = _normalize_verdict(payload["verdict"])
    findings, dropped = _parse_findings(payload, changed_lines(diff), ignored_paths)
    if verdict == APPROVE and any(
        finding.severity in BLOCKING_SEVERITIES for finding in findings
    ):
        logger.warning(
            "verdict was Approve but blocking findings survived validation; "
            "forcing %s",
            REQUEST_CHANGES,
        )
        verdict = REQUEST_CHANGES
    if verdict == REQUEST_CHANGES and not findings:
        raise VerdictError(
            "verdict is Request changes but no finding is anchored in the diff"
        )
    if dropped:
        logger.info("dropped %d finding(s) outside the diff", dropped)
    return Review(summary=summary, findings=findings, verdict=verdict, dropped=dropped)


def render_markdown(review: Review) -> str:
    """Render a validated review as the markdown posted on the PR/MR."""
    blocks = [f"## Summary\n\n{review.summary}"]
    for severity in SEVERITIES:
        findings = [
            finding for finding in review.findings if finding.severity == severity
        ]
        if not findings:
            continue
        blocks.append(f"## {SEVERITY_TITLES[severity]} findings")
        for finding in findings:
            blocks.append(_render_finding(finding))
    blocks.append(f"## Verdict\n\n**{review.verdict}**")
    return "\n\n".join(blocks)


def _render_finding(finding: Finding) -> str:
    """Render one finding as a markdown subsection."""
    location = f" — `{finding.file}:{finding.line}`" if finding.file else ""
    parts = [f"### {finding.title}{location}"]
    if finding.detail:
        parts.append(finding.detail)
    if finding.impact:
        parts.append(f"**Impact**: {finding.impact}")
    parts.append(f"**Fix**: {finding.fix or 'N/A'}")
    return "\n\n".join(parts)
