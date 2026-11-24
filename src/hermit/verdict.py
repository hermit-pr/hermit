"""Structured review verdict: parsing, validation, and rendering.

The reviewer is asked for one JSON block with a mandatory ``summary`` and one
``anchor`` per finding: an exact quote of a line the diff added or removed. That
gives the bot a deterministic check on scope, independent of the model's
judgement.

The gate is deliberately honest about its own filtering:

- an anchor that matches no changed line is downgraded to ``low`` when the
  finding names a file the diff touched, and discarded otherwise;
- an anchor that matches another changed file than the one named is downgraded
  too; the intended cross-file case, where the finding names a file outside the
  diff, is kept;
- discarded and downgraded findings are counted and stated in the summary;
- a review with no verified finding is ``Review inconclusive`` rather than a
  silent ``Approve`` on a large diff, and ``Request changes`` with nothing
  anchored no longer fails the job.
"""

import json
import logging
import posixpath
import re
from dataclasses import dataclass

from hermit.diff import changed_files, changed_line_count, locate_anchor
from hermit.protections import LostProtection, lost_protections

logger = logging.getLogger(__name__)

SEVERITIES = ("critical", "medium", "low")
SEVERITY_TITLES = {"critical": "Critical", "medium": "Medium", "low": "Low"}
BLOCKING_SEVERITIES = ("critical", "medium")

APPROVE = "Approve"
REQUEST_CHANGES = "Request changes"
INCONCLUSIVE = "Review inconclusive"
VALID_VERDICTS = (APPROVE, REQUEST_CHANGES, INCONCLUSIVE)

#: A diff at or above this many changed lines cannot be silently approved with
#: zero findings: an empty result is then an unverified result.
INCONCLUSIVE_MIN_CHANGED_LINES = 60

#: Maximum characters of model output quoted in an inconclusive review.
INCONCLUSIVE_OUTPUT_TAIL_CHARS = 1200

_SEVERITY_ALIASES = {
    "critical": "critical",
    "blocker": "critical",
    "high": "critical",
    "error": "critical",
    "medium": "medium",
    "moderate": "medium",
    "warning": "medium",
    "low": "low",
    "minor": "low",
    "info": "low",
    "nit": "low",
}

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
    relaxed: bool = False


@dataclass(frozen=True)
class Review:
    """A validated review ready to be rendered as markdown."""

    summary: str
    findings: tuple[Finding, ...]
    verdict: str
    dropped: int = 0
    relaxed: int = 0


def inconclusive_review(output: str, reason: str = "") -> Review:
    """Build an inconclusive review that preserves the failed model output.

    A model that answers in prose is a review result, not an infrastructure
    failure: publishing an inconclusive verdict keeps the review visible and
    the commit status neutral instead of losing the evidence with the pod.

    Args:
        output: The model output that could not be parsed as a verdict.
        reason: Why parsing failed, shown to the reader.
    """
    explanation = "Hermit could not extract a structured verdict from the model output"
    if reason:
        explanation += f" ({reason})"
    explanation += ". This is not an approval."
    tail = output.strip()[-INCONCLUSIVE_OUTPUT_TAIL_CHARS:]
    if tail:
        quoted = "\n".join(f"> {line}" for line in tail.splitlines())
        explanation += f"\n\nModel output (tail):\n\n{quoted}"
    return Review(summary=explanation, findings=(), verdict=INCONCLUSIVE)


def nothing_to_review() -> Review:
    """Return the neutral review published when base and head have no diff.

    A change that was already merged produces an empty diff against the current
    target branch. That is a fact about the review, not a bot failure: publish
    it as inconclusive so the commit status stays green and no review is
    silently missing.
    """
    return Review(
        summary=(
            "Nothing to review: the target branch and the proposed head have "
            "no diff. The change may already have been merged. This is not an "
            "approval."
        ),
        findings=(),
        verdict=INCONCLUSIVE,
    )


def _normalize_file(path: str) -> str:
    """Normalize a model-provided path for comparison with the diff."""
    cleaned = path.strip().strip("`").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith(("a/", "b/")):
        cleaned = cleaned[2:]
    return posixpath.normpath(cleaned) if cleaned else ""


def _same_file(left: str, right: str) -> bool:
    """Return ``True`` when two paths name the same repository file."""
    return (
        bool(left) and bool(right) and _normalize_file(left) == _normalize_file(right)
    )


def _normalize_severity(value: object) -> str:
    """Map the model's severity wording onto critical/medium/low.

    An unrecognised word is advisory, not blocking: a ``suggestion`` or
    ``concern`` must not be promoted to a medium finding and force a change.
    """
    text = str(value or "").strip().lower()
    return _SEVERITY_ALIASES.get(text, "low")


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
    """Map the model's verdict text onto the supported verdicts."""
    text = str(value).strip().lower()
    if "inconclusive" in text or "not verified" in text:
        return INCONCLUSIVE
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


def _iter_entries(payload: dict) -> list[tuple[str | None, object]]:
    """Flatten the supported finding containers into ``(section, entry)``.

    The documented shape is one array per severity, but models also emit a
    top-level ``findings`` array with a ``severity`` field, or a mapping of
    severity to entries. All three are read; malformed entries are counted by
    the caller instead of being silently skipped.
    """
    entries: list[tuple[str | None, object]] = []
    sections = {severity: payload.get(severity, []) for severity in SEVERITIES}
    if any(sections.values()):
        for severity, values in sections.items():
            if not values:
                continue
            if isinstance(values, list):
                entries.extend((severity, entry) for entry in values)
            else:
                entries.append((severity, values))
        return entries
    extra = payload.get("findings")
    if isinstance(extra, list):
        entries.extend((None, entry) for entry in extra)
    elif isinstance(extra, dict):
        for severity, values in extra.items():
            if isinstance(values, list):
                entries.extend((severity, entry) for entry in values)
            else:
                entries.append((severity, values))
    return entries


@dataclass(frozen=True)
class _AnchorInfo:
    """Where a finding's anchor resolved, and whether it was downgraded."""

    file: str
    line: int
    downgrade: str
    verified: bool


def _resolve_anchor(
    file: str, anchor: str, diff: str, diff_files: tuple[str, ...]
) -> _AnchorInfo:
    """Resolve *anchor* against the diff, flagging a missing or misanchored one."""
    matches = locate_anchor(anchor, diff)
    named_in_diff = any(_same_file(file, changed) for changed in diff_files)
    in_named = (
        [match for match in matches if _same_file(match.file, file)] if file else []
    )
    chosen = in_named[0] if in_named else (matches[0] if matches else None)
    if chosen is None:
        downgrade = "unanchored" if named_in_diff else ""
        return _AnchorInfo(file="", line=0, downgrade=downgrade, verified=named_in_diff)
    downgrade = "misanchored" if named_in_diff and not in_named else ""
    return _AnchorInfo(
        file=chosen.file, line=chosen.line, downgrade=downgrade, verified=True
    )


def _build_finding(entry: dict, section: str | None, anchor: _AnchorInfo) -> Finding:
    """Build one finding, resolving its location from the anchor when possible."""
    severity = _normalize_severity(entry.get("severity") or section)
    title = str(entry.get("title") or "Untitled")
    file = str(entry.get("file") or "")
    if anchor.downgrade:
        severity = "low"
        title = f"[{anchor.downgrade}] {title}"
    line = entry.get("line", 0)
    line = line if isinstance(line, int) else 0
    if file and anchor.file and _same_file(file, anchor.file) and anchor.line:
        line = anchor.line
    elif not file and anchor.file:
        file = anchor.file
        line = anchor.line
    return Finding(
        severity=severity,
        title=title,
        detail=str(entry.get("detail") or ""),
        fix=str(entry.get("fix") or ""),
        impact=str(entry.get("impact") or ""),
        file=file,
        line=line,
        anchor=str(entry.get("anchor") or "").strip(),
        relaxed=bool(anchor.downgrade),
    )


def _parse_findings(
    payload: dict, diff: str, ignored_paths: tuple[str, ...]
) -> tuple[tuple[Finding, ...], int, int]:
    """Build findings, downgrading or dropping those the diff cannot verify.

    Returns:
        ``(findings, dropped, relaxed)`` where ``dropped`` counts findings
        discarded entirely and ``relaxed`` counts findings kept as ``low``
        because their anchor was missing or pointed at another changed file.
    """
    findings: list[Finding] = []
    diff_files = changed_files(diff)
    dropped = 0
    relaxed = 0
    for section, entry in _iter_entries(payload):
        if not isinstance(entry, dict):
            dropped += 1
            continue
        file = str(entry.get("file") or "")
        if _matches_ignored(file, ignored_paths):
            dropped += 1
            logger.warning("dropping finding on hermit scratch path %r", file)
            continue
        anchor = _resolve_anchor(file, str(entry.get("anchor") or ""), diff, diff_files)
        if not anchor.verified:
            dropped += 1
            logger.warning(
                "dropping finding outside the diff: %s",
                str(entry.get("title") or "Untitled"),
            )
            continue
        finding = _build_finding(entry, section, anchor)
        findings.append(finding)
        if anchor.downgrade:
            relaxed += 1
            logger.warning(
                "downgraded finding to low (%s): %s", anchor.downgrade, finding.title
            )
    return tuple(findings), dropped, relaxed


def _verified_finding(lost: LostProtection) -> Finding:
    """Turn a computed lost protection into a medium finding."""
    return Finding(
        severity="medium",
        title=lost.title,
        detail=lost.detail,
        fix=lost.fix,
        impact=lost.impact,
        file=lost.file,
        line=lost.line,
        anchor=lost.anchor,
    )


def _already_reported(lost: LostProtection, findings: list[Finding]) -> bool:
    """Return ``True`` when the model already reported *lost* itself."""
    for finding in findings:
        if lost.file and finding.file and not _same_file(lost.file, finding.file):
            continue
        text = f"{finding.title} {finding.detail} {finding.impact}".lower()
        if any(needle.lower() in text for needle in lost.needles):
            return True
    return False


def _with_verified_findings(
    findings: list[Finding], diff: str
) -> tuple[list[Finding], int]:
    """Append deterministic protection findings the model did not report.

    Deduplication is against the model's findings only: a deterministic
    finding's own detail quotes the removed line (``set -euo pipefail``), so
    checking the growing list would make the first entry suppress its siblings.

    Returns:
        The findings and the number of deterministic findings that were added.
    """
    model_findings = [finding for finding in findings if not finding.relaxed]
    added = 0
    for lost in lost_protections(diff):
        if not _already_reported(lost, model_findings):
            findings.append(_verified_finding(lost))
            added += 1
    return findings, added


def _derive_verdict(
    requested: str, findings: list[Finding], diff: str, relaxed: int
) -> str:
    """Re-derive the verdict from the findings that survived validation.

    A downgraded finding is not anchor-verified, so it does not count towards
    the evidence needed to approve a substantial diff.
    """
    if any(finding.severity in BLOCKING_SEVERITIES for finding in findings):
        if requested != REQUEST_CHANGES:
            logger.warning(
                "verdict was %r but blocking findings survived validation; "
                "forcing %s",
                requested,
                REQUEST_CHANGES,
            )
        return REQUEST_CHANGES
    if requested == INCONCLUSIVE:
        return INCONCLUSIVE
    verified = len(findings) - relaxed
    if requested == REQUEST_CHANGES:
        return REQUEST_CHANGES if verified else INCONCLUSIVE
    if verified == 0 and changed_line_count(diff) >= INCONCLUSIVE_MIN_CHANGED_LINES:
        return INCONCLUSIVE
    return APPROVE


def _annotate_summary(summary: str, relaxed: int, dropped: int, verified: int) -> str:
    """State the validation losses and additions in the summary itself."""
    notes: list[str] = []
    if relaxed:
        notes.append(
            f"{relaxed} finding(s) downgraded to low because the anchor could "
            "not be verified"
        )
    if dropped:
        notes.append(
            f"{dropped} finding(s) discarded because their anchor is not part "
            "of the diff"
        )
    if verified:
        notes.append(f"{verified} finding(s) added by hermit's deterministic checks")
    if not notes:
        return summary
    return f"{summary}\n\n_Coverage: {'; '.join(notes)}._"


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
        VerdictError: if there is no JSON, no summary, or an unrecognised
            verdict. A review with zero verified findings is now published as
            ``Review inconclusive`` instead of failing the job.
    """
    payload = _json_payload(output)
    if "verdict" not in payload:
        raise VerdictError("JSON verdict has no `verdict` field")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise VerdictError("JSON verdict has no `summary`")
    requested = _normalize_verdict(payload["verdict"])
    findings, dropped, relaxed = _parse_findings(payload, diff, ignored_paths)
    findings, verified = _with_verified_findings(list(findings), diff)
    verdict = _derive_verdict(requested, findings, diff, relaxed)
    if dropped:
        logger.info("dropped %d finding(s) outside the diff", dropped)
    if relaxed:
        logger.info("downgraded %d finding(s) to low", relaxed)
    if verified:
        logger.info("added %d deterministic finding(s)", verified)
    return Review(
        summary=_annotate_summary(summary, relaxed, dropped, verified),
        findings=tuple(findings),
        verdict=verdict,
        dropped=dropped,
        relaxed=relaxed,
    )


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
    if review.verdict == INCONCLUSIVE:
        explanation = (
            "No finding was verified against the diff; this is not an approval."
            if not review.findings
            else "The review did not reach a verified verdict; this is not an "
            "approval."
        )
        blocks.append(f"## Verdict\n\n**Review inconclusive**\n\n{explanation}")
    else:
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
