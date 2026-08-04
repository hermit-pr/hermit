"""Regex-based secret detection over the raw diff before the LLM sees it.

LLMs miss hardcoded secrets that a simple regex catches reliably, so the diff
is pre-scanned and the findings are handed to the model as candidates it must
classify as true or false positives.
"""

import re
from typing import List

PLACEHOLDERS = (
    "YOUR_API_KEY",
    "CHANGE_ME",
    "EXAMPLE_SECRET",
    "REPLACE_THIS",
    "dummy_password",
)

PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"gh[op]_[a-zA-Z0-9]{36}"), "GitHub Token"),
    (re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|PGP) PRIVATE KEY-----"), "Private Key"),
    (
        re.compile(
            r"(?:password|passwd|secret|api_key|access_token)\s*[:=]\s*"
            r"['\"]?(?P<secret>[^\s]{8,})['\"]?"
        ),
        "Generic secret",
    ),
]


def scan_for_secrets(diff: str) -> List[str]:
    """Return the secret candidates found in ``diff``.

    Placeholder values such as ``CHANGE_ME`` are ignored so sample snippets do
    not trip the scanner.

    Returns:
        A list of human-readable findings, empty when nothing matches.
    """
    findings: List[str] = []
    for pattern, label in PATTERNS:
        for match in pattern.finditer(diff):
            value = (
                match.group("secret")
                if "secret" in pattern.groupindex
                else match.group(0)
            )
            value = value.strip("'\"")
            if value in PLACEHOLDERS:
                continue
            findings.append(f"{label}: {value}")
    return findings
