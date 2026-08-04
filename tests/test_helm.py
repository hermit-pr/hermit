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
