"""Spawner abstraction over the Kubernetes API for reviewer pods."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

from hermit import __version__
from hermit.config import Settings
from hermit.jobs import ReviewJob

logger = logging.getLogger(__name__)


def pod_environment(settings: Settings, job: ReviewJob) -> dict[str, str]:
    """Return the non-secret environment variables for a reviewer pod."""
    opencode_bin = settings.opencode_bin
    if settings.opencode_init_image:
        opencode_bin = "/opencode-bin/opencode"
    return {
        "HERMIT_JOB_ID": job.id,
        "HERMIT_VERSION": __version__,
        "HERMIT_GIT_PROVIDER": settings.git_provider,
        "HERMIT_GIT_HOST_URL": settings.git_host_url,
        "HERMIT_REPO": job.event.repo,
        "HERMIT_HEAD_SHA": job.event.head_sha,
        "HERMIT_HEAD_REF": job.event.head_ref,
        "HERMIT_BASE_SHA": job.event.base_sha,
        "HERMIT_BASE_REF": job.event.base_ref,
        "HERMIT_VLLM_ENDPOINT": settings.vllm_endpoint,
        "HERMIT_MODEL": settings.model,
        "HERMIT_REVIEW_RULES": settings.review_rules,
        "HERMIT_OPCODE_BIN": opencode_bin,
        "HERMIT_OPCODE_ARGS": " ".join(settings.opencode_args),
        "HERMIT_WORKSPACE": settings.workspace,
        "HERMIT_MASTER_URL": settings.master_url,
    }


class PodSpawner(ABC):
    """Creates and cleans up the reviewer pods for review jobs."""

    @abstractmethod
    async def spawn(
        self, job: ReviewJob, environment: dict[str, str]
    ) -> tuple[str, str]:
        """Create the reviewer pod and its secret; return (pod, secret) names."""

    @abstractmethod
    async def cleanup(self, job: ReviewJob) -> None:
        """Delete the pod and secret associated with ``job``."""

    async def aclose(self) -> None:
        """Release any resources held by the spawner."""


class FakePodSpawner(PodSpawner):
    """In-memory spawner used for local development and tests."""

    def __init__(self) -> None:
        self.spawned: list[tuple[ReviewJob, dict[str, str]]] = []
        self.cleaned: list[str] = []

    async def spawn(
        self, job: ReviewJob, environment: dict[str, str]
    ) -> tuple[str, str]:
        """Record the spawn and return deterministic names."""
        self.spawned.append((job, environment))
        return f"hermit-review-{job.id}", f"hermit-{job.id}"

    async def cleanup(self, job: ReviewJob) -> None:
        """Record the cleaned job."""
        self.cleaned.append(job.id)


class K8sPodSpawner(PodSpawner):
    """Spawns reviewer pods through the Kubernetes API in-cluster."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[object] = None

    def _api(self) -> object:
        """Return a lazily configured CoreV1Api client."""
        if self._client is not None:
            return self._client
        import kubernetes  # pylint: disable=import-outside-toplevel

        if self._settings.kube_config:
            kubernetes.config.load_kube_config(config_file=self._settings.kube_config)
        else:
            kubernetes.config.load_incluster_config()
        self._client = kubernetes.client.CoreV1Api()
        return self._client

    def _namespace(self) -> str:
        """Return the namespace for pods, defaulting to the in-cluster one."""
        if self._settings.pod_namespace:
            return self._settings.pod_namespace
        path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return "default"

    def _build_secret(self, job: ReviewJob, namespace: str) -> object:
        """Build the Kubernetes Secret for a review job."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        secret_name = f"hermit-{job.id}"
        return kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=secret_name, namespace=namespace
            ),
            type="Opaque",
            string_data={
                "read-token": self._settings.git_read_token.get_secret_value(),
                "report-secret": job.report_secret,
                **(
                    {"vllm-api-key": self._settings.vllm_api_key.get_secret_value()}
                    if self._settings.vllm_api_key
                    else {}
                ),
            },
        )

    def _build_env_vars(
        self, environment: dict[str, str], secret_name: str
    ) -> list[object]:
        """Build the environment variables for the reviewer container."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        env_vars = [
            kubernetes.client.V1EnvVar(name=name, value=value)
            for name, value in environment.items()
        ]
        env_vars.extend(
            [
                kubernetes.client.V1EnvVar(
                    name="HERMIT_GIT_READ_TOKEN",
                    value_from=kubernetes.client.V1EnvVarSource(
                        secret_key_ref=kubernetes.client.V1SecretKeySelector(
                            name=secret_name, key="read-token"
                        )
                    ),
                ),
                kubernetes.client.V1EnvVar(
                    name="HERMIT_REPORT_SECRET",
                    value_from=kubernetes.client.V1EnvVarSource(
                        secret_key_ref=kubernetes.client.V1SecretKeySelector(
                            name=secret_name, key="report-secret"
                        )
                    ),
                ),
            ]
        )
        if self._settings.vllm_api_key:
            env_vars.append(
                kubernetes.client.V1EnvVar(
                    name="HERMIT_VLLM_API_KEY",
                    value_from=kubernetes.client.V1EnvVarSource(
                        secret_key_ref=kubernetes.client.V1SecretKeySelector(
                            name=secret_name, key="vllm-api-key"
                        )
                    ),
                )
            )
        return env_vars

    def _build_init_container_and_volumes(self) -> tuple[list, list, list]:
        """Build init containers, volumes, and volume mounts if init image is set."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        init_containers = []
        volumes = []
        volume_mounts = []
        if self._settings.opencode_init_image:
            volume_mounts = [
                kubernetes.client.V1VolumeMount(
                    name="opencode-bin",
                    mount_path="/opencode-bin",
                )
            ]
            volumes = [
                kubernetes.client.V1Volume(
                    name="opencode-bin",
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
                )
            ]
            init_container = kubernetes.client.V1Container(
                name="opencode-provider",
                image=self._settings.opencode_init_image,
                command=[
                    "cp",
                    self._settings.opencode_init_bin_path,
                    "/opencode-bin/opencode",
                ],
                volume_mounts=volume_mounts,
                security_context=kubernetes.client.V1SecurityContext(
                    run_as_non_root=True
                ),
            )
            init_containers.append(init_container)
        return init_containers, volumes, volume_mounts

    async def spawn(
        self, job: ReviewJob, environment: dict[str, str]
    ) -> tuple[str, str]:
        """Create a Secret and a reviewer Pod for ``job``."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        if self._settings.git_read_token is None:
            raise ValueError(
                "HERMIT_GIT_READ_TOKEN is required to spawn a reviewer pod"
            )
        api = self._api()
        namespace = self._namespace()
        secret_name = f"hermit-{job.id}"
        pod_name = f"hermit-review-{job.id}"

        secret = self._build_secret(job, namespace)
        env_vars = self._build_env_vars(environment, secret_name)
        init_containers, volumes, volume_mounts = (
            self._build_init_container_and_volumes()
        )

        container = kubernetes.client.V1Container(
            name="reviewer",
            image=self._settings.pod_image,
            command=["python", "-m", "hermit.slave"],
            env=env_vars,
            volume_mounts=volume_mounts,
            security_context=kubernetes.client.V1SecurityContext(run_as_non_root=True),
        )
        pod = kubernetes.client.V1Pod(
            metadata=kubernetes.client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={
                    "app": "hermit-review",
                    "hermit.job": job.id,
                    "app.kubernetes.io/version": __version__,
                },
            ),
            spec=kubernetes.client.V1PodSpec(
                restart_policy="Never",
                init_containers=init_containers,
                containers=[container],
                volumes=volumes,
                service_account_name=self._settings.pod_service_account or None,
            ),
        )
        await asyncio.to_thread(api.create_namespaced_secret, namespace, secret)
        logger.debug("created secret %s in %s", secret_name, namespace)
        try:
            await asyncio.to_thread(api.create_namespaced_pod, namespace, pod)
        except Exception:
            await asyncio.to_thread(
                api.delete_namespaced_secret, secret_name, namespace
            )
            raise
        logger.info(
            "created reviewer pod %s in %s (image=%s)",
            pod_name,
            namespace,
            container.image,
        )
        return pod_name, secret_name

    async def cleanup(self, job: ReviewJob) -> None:
        """Delete the reviewer pod and its secret, ignoring not-found errors."""
        api = self._api()
        namespace = self._namespace()
        for name, delete in (
            (job.pod_name, api.delete_namespaced_pod),
            (job.secret_name, api.delete_namespaced_secret),
        ):
            if not name:
                continue
            try:
                await asyncio.to_thread(delete, name, namespace)
                logger.debug("deleted %s in %s", name, namespace)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("could not delete %s in %s", name, namespace)


def build_spawner(settings: Settings) -> PodSpawner:
    """Return the spawner selected by the settings."""
    if settings.pod_spawner == "fake":
        return FakePodSpawner()
    return K8sPodSpawner(settings)
