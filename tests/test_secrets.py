"""Tests for the regex secret pre-scan of the diff."""

from hermit.secretscan import scan_for_secrets


def test_scan_finds_github_token() -> None:
    """A classic GitHub token pattern is detected."""
    findings = scan_for_secrets("token = ghp_" + "a" * 36)
    assert any("GitHub Token" in finding for finding in findings)


def test_scan_finds_private_key_header() -> None:
    """A PEM private key header is detected."""
    findings = scan_for_secrets("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert any("Private Key" in finding for finding in findings)


def test_scan_finds_generic_secret_assignment() -> None:
    """An access_token assignment with a long value is detected."""
    findings = scan_for_secrets('api_key = "sk-0123456789abcdef"')
    assert any("Generic secret" in finding for finding in findings)


def test_scan_ignores_placeholders() -> None:
    """Placeholder values never surface as findings."""
    diff = "\n".join(
        [
            'password = "YOUR_API_KEY"',
            "secret = CHANGE_ME",
            "access_token = EXAMPLE_SECRET",
            "api_key = REPLACE_THIS",
            'passwd = "dummy_password"',
        ]
    )
    assert not scan_for_secrets(diff)


def test_scan_ignores_short_generic_values() -> None:
    """Generic secret values shorter than 8 characters are skipped."""
    diff = 'password = "short"'
    assert not scan_for_secrets(diff)
