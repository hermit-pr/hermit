"""Spawner abstraction over the Kubernetes API for reviewer pods.

The per-job Kubernetes Secret is the durable source of truth for a review: it
holds the read-only git token, the per-job report secret, and a serialized
``ChangeEvent``, and carries a ``hermit.dev/status`` annotation. Any master
replica can therefore verify a report and publish the review without any
in-memory state, which makes the master horizontally scalable and safe across
restarts. A background sweep reclaims orphaned pods and Secrets.
"""

import asyncio
import base64
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

from hermit import __version__
from hermit.config import Settings
from hermit.jobs import ReviewJob
from hermit.models import ChangeEvent

logger = logging.getLogger(__name__)

STATUS_ANNOTATION = "hermit.dev/status"
JOB_APP_LABEL = "app"
JOB_APP_VALUE = "hermit-review"
JOB_LABEL_KEY = "hermit.job"
SWEEP_INTERVAL_SECONDS = 300


class JobAlreadyExists(Exception):
    """Raised when a job for the same change event already exists."""


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
        "HERMIT_SOURCE_REPO": job.event.source_repo,
        "HERMIT_HEAD_SHA": job.event.head_sha,
        "HERMIT_HEAD_REF": job.event.head_ref,
        "HERMIT_BASE_SHA": job.event.base_sha,
        "HERMIT_BASE_REF": job.event.base_ref,
        "HERMIT_VLLM_ENDPOINT": settings.vllm_endpoint,
        "HERMIT_MODEL": settings.model,
        "HERMIT_REVIEW_RULES": settings.review_rules,
        "HERMIT_POLICY_FILE_PATH": settings.policy_file_path,
        "HERMIT_OPCODE_BIN": opencode_bin,
        "HERMIT_OPCODE_ARGS": " ".join(settings.opencode_args),
        "HERMIT_WORKSPACE": settings.workspace,
        "HERMIT_MASTER_URL": settings.master_url,
    }


class PodSpawner(ABC):
    """Creates, looks up, and cleans up reviewer pods for review jobs."""

    @abstractmethod
    async def spawn(
        self, job: ReviewJob, environment: dict[str, str]
    ) -> tuple[str, str]:
        """Create the reviewer pod and its secret; return (pod, secret) names."""

    @abstractmethod
    async def cleanup(self, job: ReviewJob) -> None:
        """Delete the pod and secret associated with ``job``."""

    @abstractmethod
    async def get_job(self, job_id: str) -> Optional[ReviewJob]:
        """Return the durable job record for ``job_id`` or ``None``."""

    @abstractmethod
    async def mark_posted(self, job_id: str) -> bool:
        """Atomically mark the job as reported.

        Returns:
            True if this call performed the transition, False if another
            replica already posted the review.
        """

    @abstractmethod
    async def mark_failed(self, job_id: str) -> None:
        """Record that the review could not be posted."""

    @abstractmethod
    async def sweep(self) -> None:
        """Reclaim finished or stale reviewer pods and their secrets."""

    async def aclose(self) -> None:
        """Release any resources held by the spawner."""


class FakePodSpawner(PodSpawner):
    """In-memory spawner used for local development and tests."""

    def __init__(self) -> None:
        self.spawned: list[tuple[ReviewJob, dict[str, str]]] = []
        self.cleaned: list[str] = []
        self.jobs: dict[str, ReviewJob] = {}

    @staticmethod
    def _pod_name(job_id: str) -> str:
        """Return the pod name used for ``job_id``."""
        return f"hermit-review-{job_id}"

    @staticmethod
    def _secret_name(job_id: str) -> str:
        """Return the secret name used for ``job_id``."""
        return f"hermit-{job_id}"

    async def spawn(
        self, job: ReviewJob, environment: dict[str, str]
    ) -> tuple[str, str]:
        """Record the spawn and return deterministic names."""
        self.spawned.append((job, environment))
        self.jobs[job.id] = job
        return self._pod_name(job.id), self._secret_name(job.id)

    async def cleanup(self, job: ReviewJob) -> None:
        """Record the cleaned job."""
        self.cleaned.append(job.id)
        self.jobs.pop(job.id, None)

    async def get_job(self, job_id: str) -> Optional[ReviewJob]:
        """Return the recorded job with ``job_id``."""
        return self.jobs.get(job_id)

    async def mark_posted(self, job_id: str) -> bool:
        """Mark the recorded job as posted."""
        job = self.jobs.get(job_id)
        if job is not None:
            job.status = "posted"
        return True

    async def mark_failed(self, job_id: str) -> None:
        """Mark the recorded job as failed."""
        job = self.jobs.get(job_id)
        if job is not None:
            job.status = "failed"

    async def sweep(self) -> None:
        """Nothing to reclaim in the fake spawner."""


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

    @staticmethod
    def _pod_name(job_id: str) -> str:
        """Return the pod name used for ``job_id``."""
        return f"hermit-review-{job_id}"

    @staticmethod
    def _secret_name(job_id: str) -> str:
        """Return the secret name used for ``job_id``."""
        return f"hermit-{job_id}"

    def _build_secret(self, job: ReviewJob, namespace: str) -> object:
        """Build the durable Kubernetes Secret for a review job."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        return kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=self._secret_name(job.id),
                namespace=namespace,
                labels={
                    JOB_APP_LABEL: JOB_APP_VALUE,
                    JOB_LABEL_KEY: job.id,
                    "app.kubernetes.io/version": __version__,
                },
                annotations={STATUS_ANNOTATION: "pending"},
            ),
            type="Opaque",
            string_data={
                "read-token": self._settings.git_read_token.get_secret_value(),
                "report-secret": job.report_secret,
                "event": job.event.model_dump_json(),
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
                    run_as_non_root=True, run_as_user=1000, run_as_group=1000
                ),
            )
            init_containers.append(init_container)
        return init_containers, volumes, volume_mounts

    def _build_pod(
        self,
        pod_name: str,
        namespace: str,
        job: ReviewJob,
        *,
        env_vars: list[object],
        init_containers: list[object],
        volumes: list[object],
        volume_mounts: list[object],
    ) -> object:
        """Build the reviewer pod specification."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        s = self._settings
        container_volumes = [
            kubernetes.client.V1VolumeMount(name="workspace", mount_path=s.workspace),
            kubernetes.client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            kubernetes.client.V1VolumeMount(name="home", mount_path="/home/hermit"),
        ]
        pod_volumes = list(volumes) + [
            kubernetes.client.V1Volume(
                name="workspace",
                empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
            ),
            kubernetes.client.V1Volume(
                name="tmp",
                empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
            ),
            kubernetes.client.V1Volume(
                name="home",
                empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
            ),
        ]
        container = kubernetes.client.V1Container(
            name="reviewer",
            image=s.pod_image,
            command=["python", "-m", "hermit.slave"],
            env=env_vars,
            volume_mounts=volume_mounts + container_volumes,
            security_context=kubernetes.client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                run_as_group=1000,
                read_only_root_filesystem=True,
                allow_privilege_escalation=False,
                capabilities=kubernetes.client.V1Capabilities(drop=["ALL"]),
            ),
            resources=kubernetes.client.V1ResourceRequirements(
                requests={"cpu": s.pod_cpu_request, "memory": s.pod_memory_request},
                limits={"cpu": s.pod_cpu_limit, "memory": s.pod_memory_limit},
            ),
        )
        return kubernetes.client.V1Pod(
            metadata=kubernetes.client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={
                    JOB_APP_LABEL: JOB_APP_VALUE,
                    JOB_LABEL_KEY: job.id,
                    "app.kubernetes.io/version": __version__,
                },
            ),
            spec=kubernetes.client.V1PodSpec(
                restart_policy="Never",
                init_containers=init_containers,
                containers=[container],
                volumes=pod_volumes,
                security_context=kubernetes.client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                    seccomp_profile=kubernetes.client.V1SeccompProfile(
                        type="RuntimeDefault"
                    ),
                ),
                service_account_name=s.pod_service_account or None,
                automount_service_account_token=False,
                active_deadline_seconds=s.report_timeout_seconds + 60,
            ),
        )

    # pylint: disable=too-many-locals
    async def spawn(
        self, job: ReviewJob, environment: dict[str, str]
    ) -> tuple[str, str]:
        """Create a Secret and a reviewer Pod for ``job``.

        The Secret is created first and acts as a cross-replica lock: a
        duplicated event produces the same deterministic job id, so a second
        replica hitting an existing Secret gets a 409 and must not spawn again.
        """
        import kubernetes  # pylint: disable=import-outside-toplevel

        api = self._api()
        namespace = self._namespace()
        secret_name = self._secret_name(job.id)
        pod_name = self._pod_name(job.id)

        secret = self._build_secret(job, namespace)
        env_vars = self._build_env_vars(environment, secret_name)
        init_containers, volumes, volume_mounts = (
            self._build_init_container_and_volumes()
        )
        pod = self._build_pod(
            pod_name,
            namespace,
            job,
            env_vars=env_vars,
            init_containers=init_containers,
            volumes=volumes,
            volume_mounts=volume_mounts,
        )
        image = self._settings.pod_image
        try:
            await asyncio.to_thread(api.create_namespaced_secret, namespace, secret)
        except kubernetes.client.ApiException as exc:
            if exc.status == 409:
                logger.info("job %s already exists; skipping duplicate", job.id)
                raise JobAlreadyExists(job.id) from exc
            raise
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
            image,
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

    @staticmethod
    def _decode(data: dict[str, str], key: str) -> str:
        """Decode a base64 ``data`` entry of a Kubernetes Secret."""
        raw = data.get(key)
        if not raw:
            return ""
        return base64.b64decode(raw).decode("utf-8")

    async def get_job(self, job_id: str) -> Optional[ReviewJob]:
        """Reconstruct the durable job record from its Secret."""
        import kubernetes  # pylint: disable=import-outside-toplevel

        api = self._api()
        namespace = self._namespace()
        name = self._secret_name(job_id)
        try:
            secret = await asyncio.to_thread(
                api.read_namespaced_secret, name, namespace
            )
        except kubernetes.client.ApiException as exc:
            if exc.status == 404:
                return None
            raise
        data = secret.data or {}
        event = ChangeEvent.model_validate_json(self._decode(data, "event"))
        status = (secret.metadata.annotations or {}).get(STATUS_ANNOTATION, "pending")
        return ReviewJob(
            id=job_id,
            event=event,
            report_secret=self._decode(data, "report-secret"),
            status=status,
            pod_name=self._pod_name(job_id),
            secret_name=name,
        )

    async def mark_posted(self, job_id: str) -> bool:
        """Atomically flip the job status to ``posted`` using a CAS replace.

        Returns ``False`` when another replica changed the secret concurrently,
        meaning this replica must not publish the review a second time.
        """
        import kubernetes  # pylint: disable=import-outside-toplevel

        api = self._api()
        namespace = self._namespace()
        name = self._secret_name(job_id)
        secret = await asyncio.to_thread(api.read_namespaced_secret, name, namespace)
        annotations = dict(secret.metadata.annotations or {})
        if annotations.get(STATUS_ANNOTATION) == "posted":
            return False
        annotations[STATUS_ANNOTATION] = "posted"
        secret.metadata.annotations = annotations
        try:
            await asyncio.to_thread(
                api.replace_namespaced_secret, name, namespace, secret
            )
        except kubernetes.client.ApiException as exc:
            if exc.status == 409:
                return False
            raise
        return True

    async def mark_failed(self, job_id: str) -> None:
        """Best-effort record that the review could not be posted."""
        api = self._api()
        namespace = self._namespace()
        name = self._secret_name(job_id)
        try:
            secret = await asyncio.to_thread(
                api.read_namespaced_secret, name, namespace
            )
            annotations = dict(secret.metadata.annotations or {})
            annotations[STATUS_ANNOTATION] = "failed"
            secret.metadata.annotations = annotations
            await asyncio.to_thread(
                api.replace_namespaced_secret, name, namespace, secret
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("could not mark job %s failed", job_id)

    async def sweep(self) -> None:
        """Reclaim finished or stale reviewer pods and orphaned Secrets."""
        api = self._api()
        namespace = self._namespace()
        cutoff = time.time() - self._settings.report_timeout_seconds
        try:
            pods = await asyncio.to_thread(
                api.list_namespaced_pod,
                namespace,
                label_selector=f"{JOB_APP_LABEL}={JOB_APP_VALUE}",
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("pod sweep failed in %s", namespace)
            return
        for pod in pods.items or []:
            finished = (pod.status.phase or "") in ("Succeeded", "Failed")
            created = pod.metadata.creation_timestamp
            stale = created is not None and created.timestamp() < cutoff
            if not (finished or stale):
                continue
            name = pod.metadata.name
            job_id = (pod.metadata.labels or {}).get(JOB_LABEL_KEY)
            try:
                await asyncio.to_thread(api.delete_namespaced_pod, name, namespace)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("could not delete pod %s", name)
            if job_id:
                try:
                    await asyncio.to_thread(
                        api.delete_namespaced_secret,
                        self._secret_name(job_id),
                        namespace,
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.debug("could not delete secret for job %s", job_id)
        try:
            secrets = await asyncio.to_thread(
                api.list_namespaced_secret,
                namespace,
                label_selector=f"{JOB_APP_LABEL}={JOB_APP_VALUE}",
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("secret sweep failed in %s", namespace)
            return
        for secret in secrets.items or []:
            created = secret.metadata.creation_timestamp
            if created is not None and created.timestamp() < cutoff:
                try:
                    await asyncio.to_thread(
                        api.delete_namespaced_secret, secret.metadata.name, namespace
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.debug("could not delete secret %s", secret.metadata.name)


def build_spawner(settings: Settings) -> PodSpawner:
    """Return the spawner selected by the settings."""
    if settings.pod_spawner == "fake":
        return FakePodSpawner()
    return K8sPodSpawner(settings)
