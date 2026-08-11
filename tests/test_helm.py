"""Tests for the Helm chart templates (static checks)."""

from pathlib import Path

HELM_DIR = Path(__file__).resolve().parents[1] / "helm" / "hermit"


def _template(name: str) -> str:
    """Return the text of a chart template file."""
    return (HELM_DIR / "templates" / name).read_text(encoding="utf-8")


def test_configmap_generates_default_master_url() -> None:
    """masterUrl defaults to the release service URL when unset."""
    configmap = _template("configmap.yaml")
    assert (
        "HERMIT_MASTER_URL: {{ .Values.config.masterUrl | default "
        '(printf "http://%s:%d" (include "hermit.fullname" .) (int '
        ".Values.service.port)) | quote }}"
    ) in configmap


def test_configmap_exports_policy_and_pod_resources() -> None:
    """The ConfigMap passes the policy file path and pod resources through."""
    configmap = _template("configmap.yaml")
    assert "HERMIT_POLICY_FILE_PATH" in configmap
    assert "HERMIT_POD_CPU_REQUEST" in configmap
    assert "HERMIT_POD_MEMORY_LIMIT" in configmap


def test_configmap_only_sets_review_rules_when_configured() -> None:
    """Empty reviewRules leaves the env var out so the bot default is used."""
    configmap = _template("configmap.yaml")
    assert "if .Values.config.reviewRules" in configmap
    values = (HELM_DIR / "values.yaml").read_text(encoding="utf-8")
    assert 'reviewRules: ""' in values


def test_deployment_has_container_security_context() -> None:
    """The master container drops all capabilities and is read-only root."""
    deployment = _template("deployment.yaml")
    assert "allowPrivilegeEscalation: false" in deployment
    assert "readOnlyRootFilesystem: true" in deployment
    assert "drop:" in deployment


def test_deployment_uses_job_id_signing_key_env() -> None:
    """The signing key env var matches the renamed setting."""
    deployment = _template("deployment.yaml")
    assert "HERMIT_JOB_ID_SIGNING_KEY" in deployment


def test_values_include_seccomp_profile() -> None:
    """The default pod security context pins a RuntimeDefault seccomp profile."""
    values = (HELM_DIR / "values.yaml").read_text(encoding="utf-8")
    assert "RuntimeDefault" in values


def test_supports_private_ca_bundle() -> None:
    """The chart can inject a private CA bundle for airgapped PKI."""
    configmap = _template("configmap.yaml")
    deployment = _template("deployment.yaml")
    values = (HELM_DIR / "values.yaml").read_text(encoding="utf-8")
    assert "caBundle" in values
    assert "HERMIT_CA_BUNDLE_PATH" in configmap
    assert "HERMIT_POD_CA_MOUNT_PATH" in configmap
    assert "ca-bundle" in deployment
    assert "SSL_CERT_FILE" in deployment


def test_deployment_supports_per_org_rw_tokens() -> None:
    """The deployment template generates HERMIT_GITHUB_RW_ORG_ env vars."""
    deployment = _template("deployment.yaml")
    assert "HERMIT_GITHUB_RW_ORG_" in deployment
    assert "_NAME" in deployment
    assert "_TOKEN" in deployment
    assert "githubRwToken.orgs" in deployment


def test_deployment_supports_per_org_read_tokens() -> None:
    """The deployment template generates HERMIT_GIT_READ_ORG_ env vars."""
    deployment = _template("deployment.yaml")
    assert "HERMIT_GIT_READ_ORG_" in deployment


def test_deployment_injects_fallback_tokens_via_secret_key_ref() -> None:
    """Fallback tokens are injected via secretKeyRef."""
    deployment = _template("deployment.yaml")
    assert "HERMIT_GITHUB_TOKEN" in deployment
    assert "HERMIT_GIT_READ_TOKEN" in deployment
    assert "githubRwToken.token.secretName" in deployment
    assert "githubReadToken.token.secretName" in deployment


def test_values_has_no_inline_tokens() -> None:
    """Token values.yaml refs Secret by name+key — never inline values."""
    values = (HELM_DIR / "values.yaml").read_text(encoding="utf-8")
    assert "orgs:" in values
    assert "  # - name:" in values  # commented example, not inline value
    assert 'secretName: ""' in values
    assert 'secretKey: ""' in values
    assert "orgs: []" in values
    assert "orgs: {}" not in values  # old inline map format is gone


def test_secrets_tokens_file_is_absent() -> None:
    """secrets-tokens.yaml was deleted — tokens are injected via secretKeyRef."""
    assert not (HELM_DIR / "templates" / "secrets-tokens.yaml").exists()
