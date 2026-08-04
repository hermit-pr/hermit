"""Tests for the Kubernetes pod spawner."""

from unittest.mock import MagicMock, patch

import kubernetes
import pytest

from hermit import __version__
from hermit.config import Settings
from hermit.jobs import ReviewJob
from hermit.k8s import JobAlreadyExists, K8sPodSpawner, pod_environment
from hermit.models import ChangeEvent


def make_job(**overrides) -> ReviewJob:
    """Create a test ReviewJob with sensible defaults."""
    defaults = {
        "id": "test-job-123",
        "event": ChangeEvent(
            provider="github",
            action="opened",
            repo="owner/repo",
            ref="refs/heads/feature/test",
            head_sha="abc123",
            head_ref="feature/test",
            base_sha="def456",
            base_ref="main",
        ),
        "report_secret": "secret123",
    }
    defaults.update(overrides)
    return ReviewJob(**defaults)


def make_settings(**overrides) -> Settings:
    """Create test Settings with sensible defaults."""
    defaults = {
        "webhook_secret": "0123456789abcdef",
        "git_read_token": "read-token",
        "github_token": "github-token",
        "vllm_endpoint": "http://vllm.example:8000/v1",
        "model": "test-model",
        "master_url": "http://hermit:8080",
        "pod_image": "hermit:test",
        "opencode_init_image": "ghcr.io/anomalyco/opencode:latest",
        "opencode_init_bin_path": "/usr/local/bin/opencode",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_pod_environment_without_init_image() -> None:
    """pod_environment uses opencode_bin when init image is not set."""
    settings = make_settings(opencode_init_image="", opencode_bin="custom-opencode")
    job = make_job()
    env = pod_environment(settings, job)
    assert env["HERMIT_OPCODE_BIN"] == "custom-opencode"


def test_pod_environment_includes_version() -> None:
    """pod_environment always exports the code version, not an env override."""
    settings = make_settings()
    job = make_job()
    env = pod_environment(settings, job)
    assert env["HERMIT_VERSION"] == __version__


def test_pod_environment_with_init_image() -> None:
    """pod_environment uses mounted path when init image is set."""
    settings = make_settings(opencode_init_image="ghcr.io/anomalyco/opencode:latest")
    job = make_job()
    env = pod_environment(settings, job)
    assert env["HERMIT_OPCODE_BIN"] == "/opencode-bin/opencode"


def test_pod_environment_sets_ca_trust_env_when_configured() -> None:
    """Reviewer pods inherit the CA trust variables when a bundle is set."""
    settings = make_settings(ca_bundle_path="/etc/hermit/ca/ca.pem")
    env = pod_environment(settings, make_job())
    for var in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert env[var] == "/etc/hermit/ca/ca.pem"


@pytest.mark.asyncio
async def test_k8s_pod_mounts_ca_bundle_from_configmap() -> None:
    """A configured private CA bundle is mounted from a ConfigMap."""
    settings = make_settings(
        ca_bundle_path="/etc/hermit/ca/ca.pem",
        pod_ca_mount_path="/etc/hermit/ca",
        pod_ca_configmap="my-ca",
    )
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        _, pod = mock_api.create_namespaced_pod.call_args[0]
        mounts = {m.name: m for m in pod.spec.containers[0].volume_mounts}
        assert mounts["ca-bundle"].mount_path == "/etc/hermit/ca"
        assert mounts["ca-bundle"].read_only is True
        volumes = {v.name: v for v in pod.spec.volumes}
        assert volumes["ca-bundle"].config_map.name == "my-ca"


@pytest.mark.asyncio
async def test_k8s_pod_mounts_ca_bundle_from_secret() -> None:
    """A configured private CA bundle is mounted from a Secret."""
    settings = make_settings(
        ca_bundle_path="/etc/hermit/ca/ca.pem",
        pod_ca_mount_path="/etc/hermit/ca",
        pod_ca_secret="my-ca-secret",
    )
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        _, pod = mock_api.create_namespaced_pod.call_args[0]
        volumes = {v.name: v for v in pod.spec.volumes}
        assert volumes["ca-bundle"].secret.secret_name == "my-ca-secret"


@pytest.mark.asyncio
async def test_k8s_spawner_uses_code_version_label() -> None:
    """K8sPodSpawner labels reviewer pods with the code version."""
    settings = make_settings()
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        call_args = mock_api.create_namespaced_pod.call_args
        pod = call_args[0][1]  # Second positional arg is the pod body
        assert pod.metadata.labels["app.kubernetes.io/version"] == __version__


@pytest.mark.asyncio
async def test_k8s_pod_is_hardened() -> None:
    """Reviewer pods are bounded, non-root, and carry no cluster identity."""
    settings = make_settings()
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        _, pod = mock_api.create_namespaced_pod.call_args[0]
        assert pod.spec.automount_service_account_token is False
        assert pod.spec.active_deadline_seconds == settings.report_timeout_seconds + 60


@pytest.mark.asyncio
async def test_k8s_pod_has_resources_and_security_context() -> None:
    """Reviewer pods carry resource bounds and a locked-down security context."""
    settings = make_settings()
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        _, pod = mock_api.create_namespaced_pod.call_args[0]
        container = pod.spec.containers[0]

        assert container.resources.requests["cpu"] == "200m"
        assert container.resources.requests["memory"] == "512Mi"
        assert container.resources.limits["cpu"] == "1"
        assert container.resources.limits["memory"] == "2Gi"

        context = container.security_context
        assert context.run_as_non_root is True
        assert context.run_as_user == 1000
        assert context.run_as_group == 1000
        assert context.read_only_root_filesystem is True
        assert context.allow_privilege_escalation is False
        assert context.capabilities.drop == ["ALL"]

        pod_context = pod.spec.security_context
        assert pod_context.run_as_user == 1000
        assert pod_context.run_as_group == 1000
        assert pod_context.fs_group == 1000
        assert pod_context.seccomp_profile.type == "RuntimeDefault"


@pytest.mark.asyncio
async def test_k8s_job_secret_is_durable() -> None:
    """The job secret carries the event, labels, and a status annotation."""
    settings = make_settings()
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        _, secret = mock_api.create_namespaced_secret.call_args[0]
        assert secret.string_data["report-secret"] == job.report_secret
        assert secret.metadata.labels["hermit.job"] == job.id
        assert secret.metadata.annotations["hermit.dev/status"] == "pending"
        assert '"ref":"refs/heads/feature/test"' in secret.string_data["event"]


@pytest.mark.asyncio
async def test_k8s_spawner_raises_duplicate_on_conflict() -> None:
    """A 409 on the job secret means a duplicate event is rejected."""
    settings = make_settings()
    job = make_job()
    conflict = kubernetes.client.ApiException(
        http_resp=MagicMock(status=409, reason="Conflict", data=b"conflict")
    )

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock(side_effect=conflict)

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        with pytest.raises(JobAlreadyExists):
            await spawner.spawn(job, pod_environment(settings, job))
        mock_api.create_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_k8s_spawner_embeds_vllm_api_key_when_configured() -> None:
    """K8sPodSpawner puts the vLLM API key in the job secret and env."""
    settings = make_settings(vllm_api_key="vllm-secret")
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        _, secret = mock_api.create_namespaced_secret.call_args[0]
        assert secret.string_data["vllm-api-key"] == "vllm-secret"

        _, pod = mock_api.create_namespaced_pod.call_args[0]
        env = {var.name: var for var in pod.spec.containers[0].env}
        ref = env["HERMIT_VLLM_API_KEY"].value_from.secret_key_ref
        assert ref.name == f"hermit-{job.id}"
        assert ref.key == "vllm-api-key"


@pytest.mark.asyncio
async def test_k8s_spawner_adds_init_container_when_enabled() -> None:
    """K8sPodSpawner adds init container and volume when opencode_init_image is set."""
    settings = make_settings()
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        # Verify pod was created with init container
        mock_api.create_namespaced_pod.assert_called_once()
        call_args = mock_api.create_namespaced_pod.call_args
        pod = call_args[0][1]  # Second positional arg is the pod body

        # Check init container exists
        assert pod.spec.init_containers is not None
        assert len(pod.spec.init_containers) == 1
        init_container = pod.spec.init_containers[0]
        assert init_container.name == "opencode-provider"
        assert init_container.image == "ghcr.io/anomalyco/opencode:latest"
        assert init_container.command == [
            "cp",
            "/usr/local/bin/opencode",
            "/opencode-bin/opencode",
        ]

        # Check volume mount on init container
        assert init_container.volume_mounts is not None
        assert len(init_container.volume_mounts) == 1
        assert init_container.volume_mounts[0].name == "opencode-bin"
        assert init_container.volume_mounts[0].mount_path == "/opencode-bin"

        # Check main container has volume mounts
        assert pod.spec.containers[0].volume_mounts is not None
        mounts = {m.name: m for m in pod.spec.containers[0].volume_mounts}
        assert mounts["opencode-bin"].mount_path == "/opencode-bin"
        assert mounts["workspace"].mount_path == "/workspace"
        assert mounts["tmp"].mount_path == "/tmp"
        assert mounts["home"].mount_path == "/home/hermit"

        # Check volumes exist
        assert pod.spec.volumes is not None
        names = {v.name: v for v in pod.spec.volumes}
        assert names["opencode-bin"].empty_dir is not None
        assert names["workspace"].empty_dir is not None
        assert names["tmp"].empty_dir is not None
        assert names["home"].empty_dir is not None


@pytest.mark.asyncio
async def test_k8s_spawner_no_init_container_when_disabled() -> None:
    """K8sPodSpawner does not add init container when opencode_init_image is empty."""
    settings = make_settings(opencode_init_image="")
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        # Verify pod was created without init container
        mock_api.create_namespaced_pod.assert_called_once()
        call_args = mock_api.create_namespaced_pod.call_args
        pod = call_args[0][1]  # Second positional arg is the pod body

        # Check no init containers
        assert pod.spec.init_containers is None or len(pod.spec.init_containers) == 0

        # Check the runtime volumes are still present
        assert pod.spec.volumes is not None
        names = {v.name for v in pod.spec.volumes}
        assert names == {"workspace", "tmp", "home"}

        # Check main container only mounts the runtime volumes
        assert pod.spec.containers[0].volume_mounts is not None
        mounts = {m.name for m in pod.spec.containers[0].volume_mounts}
        assert mounts == {"workspace", "tmp", "home"}


@pytest.mark.asyncio
async def test_k8s_spawner_custom_init_image_and_bin_path() -> None:
    """K8sPodSpawner uses custom init image and bin path when configured."""
    settings = make_settings(
        opencode_init_image="my-registry/opencode:v1.0",
        opencode_init_bin_path="/custom/path/opencode",
    )
    job = make_job()

    with patch("kubernetes.client.CoreV1Api") as mock_api_class:
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.create_namespaced_secret = MagicMock()
        mock_api.create_namespaced_pod = MagicMock()

        spawner = K8sPodSpawner(settings)
        spawner._client = mock_api  # pylint: disable=protected-access

        await spawner.spawn(job, pod_environment(settings, job))

        mock_api.create_namespaced_pod.assert_called_once()
        call_args = mock_api.create_namespaced_pod.call_args
        pod = call_args[0][1]  # Second positional arg is the pod body

        init_container = pod.spec.init_containers[0]
        assert init_container.image == "my-registry/opencode:v1.0"
        assert init_container.command == [
            "cp",
            "/custom/path/opencode",
            "/opencode-bin/opencode",
        ]
