"""Application settings, loaded from environment variables.

H.E.R.M.I.T reads its configuration from environment variables prefixed with
``HERMIT_``. A local ``.env`` file is also supported for development.
"""

import shlex
from functools import lru_cache
from typing import List, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GitProvider = Literal["github", "gitlab"]

DEFAULT_REVIEW_RULES = (
    "Write the review as a single markdown comment. Start directly with the "
    "first section heading — no preamble, no meta-commentary, no thinking, "
    "no 'Let me check...' or 'Looking at this PR...'. Fill every section. "
    "If a section has no findings, write 'None.'\n\n"
    "## Critical changes that need to be fixed\n\n\n"
    "## Medium issues\n\n\n"
    "## Low issues\n\n\n"
    "## General feedback\n\n\n"
    "Judge correctness, security, maintainability, and consistency. "
    "For each finding explain the risk concisely with a concrete fix.\n"
    "What to look for:\n"
    "- Bugs, security holes, crashes → Critical.\n"
    "- Logic problems, missing edge cases, silent breakage → Critical or "
    "Medium depending on blast radius.\n"
    "- Incomplete changes: are all related files updated? Deleted files "
    "cleaned up? Handlers, variables, callers, cross-file references "
    "in sync? → Medium.\n"
    "- Idempotency and safety → Medium.\n"
    "- Dry-run and error behaviour → Medium.\n"
    "- Consistency with existing patterns → Low.\n"
    "- Secrets audit: classify as [TRUE POSITIVE] or [FALSE POSITIVE].\n"
    "- Stale references to deleted/renamed symbols → Medium.\n\n"
    "Be concise — a one-line issue with a fix is better than a paragraph."
)


class _SettingsBase(BaseSettings):
    """Shared configuration behaviour for both H.E.R.M.I.T roles."""

    model_config = SettingsConfigDict(
        env_prefix="HERMIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("opencode_args", mode="before", check_fields=False)
    @classmethod
    def _parse_opencode_args(cls, value: object) -> object:
        """Split a whitespace separated argument string into a list."""
        if isinstance(value, str):
            return shlex.split(value)
        return value


class Settings(_SettingsBase):
    """Runtime settings for the H.E.R.M.I.T master.

    Every field maps to an ``HERMIT_<NAME>`` environment variable. Values that
    must never be committed, such as tokens, are typed as ``SecretStr``.
    """

    git_provider: GitProvider = "github"
    git_host_url: str = "https://github.com"

    github_token: SecretStr | None = None
    gitlab_token: SecretStr | None = None
    git_read_token: SecretStr

    webhook_secret: SecretStr = Field(..., min_length=16)
    job_id_signing_key: SecretStr | None = None
    vllm_endpoint: str = Field(..., description="URL of the vLLM inference endpoint")
    model: str = Field(..., description="Model served by the vLLM endpoint")
    review_rules: str = ""
    vllm_api_key: SecretStr | None = None
    policy_file_path: str = "AGENTS.md"
    trigger_tags: List[str] = Field(default_factory=lambda: ["@hermit", "/recheck"])

    rate_limit_per_ip: int = 60
    rate_limit_global: int = 600
    rate_limit_window_seconds: int = 60
    trust_x_forwarded_for: bool = False
    max_body_bytes: int = 10 * 1024 * 1024

    pod_cpu_request: str = "200m"
    pod_memory_request: str = "512Mi"
    pod_cpu_limit: str = "1"
    pod_memory_limit: str = "2Gi"

    opencode_bin: str = "opencode"
    opencode_args: List[str] = Field(
        default_factory=lambda: ["run", "--auto", "--format", "json"]
    )
    opencode_init_image: str = "ghcr.io/anomalyco/opencode:latest"
    opencode_init_bin_path: str = "/usr/local/bin/opencode"
    opencode_init_image_pull_policy: str = "IfNotPresent"
    opencode_init_resources: dict = Field(
        default_factory=lambda: {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        }
    )
    opencode_timeout_seconds: int = 900
    workspace: str = "/workspace"

    master_url: str = Field(..., description="URL reviewer pods use to report back")
    pod_image: str = Field(..., description="Image to run reviewer pods")
    pod_namespace: str = ""
    pod_service_account: str = ""
    pod_image_pull_policy: str = "IfNotPresent"
    report_timeout_seconds: int = 1800
    abandoned_job_timeout_seconds: int = 3600
    max_concurrent_jobs: int = 20
    pod_spawner: Literal["k8s", "fake"] = "k8s"
    kube_config: str | None = None

    # Private PKI support for airgapped deployments. When ``ca_bundle_path``
    # is set, reviewer pods receive a read-only mount of the CA bundle and the
    # trust environment variables (``SSL_CERT_FILE``, ``REQUESTS_CA_BUNDLE``,
    # ``GIT_SSL_CAINFO``, ``CURL_CA_BUNDLE``, ``NODE_EXTRA_CA_CERTS``).
    ca_bundle_path: str = ""
    pod_ca_configmap: str = ""
    pod_ca_secret: str = ""
    pod_ca_mount_path: str = ""

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"

    @model_validator(mode="after")
    def _validate_tokens(self) -> "Settings":
        """Fail fast when the token for the configured provider is missing."""
        if self.git_provider == "github" and not self.github_token:
            raise ValueError(
                "HERMIT_GITHUB_TOKEN is required when git_provider is 'github'"
            )
        if self.git_provider == "gitlab" and not self.gitlab_token:
            raise ValueError(
                "HERMIT_GITLAB_TOKEN is required when git_provider is 'gitlab'"
            )
        return self


class SlaveSettings(_SettingsBase):
    """Settings for a single reviewer (slave) pod.

    The master fills these from the originating change event; the slave only
    ever reads them from its own environment.
    """

    job_id: str
    git_provider: GitProvider = "github"
    git_host_url: str = "https://github.com"
    git_read_token: SecretStr
    repo: str
    ref: str = ""
    pr_title: str = ""
    pr_body: str = ""
    source_repo: str = ""
    head_sha: str = ""
    head_ref: str = ""
    base_sha: str = ""
    base_ref: str = ""
    vllm_endpoint: str
    model: str
    review_rules: str = ""
    vllm_api_key: SecretStr | None = None
    policy_file_path: str = "AGENTS.md"
    opencode_bin: str = "opencode"
    opencode_args: List[str] = Field(
        default_factory=lambda: ["run", "--auto", "--format", "json"]
    )
    opencode_init_image: str = "ghcr.io/anomalyco/opencode:latest"
    opencode_init_bin_path: str = "/usr/local/bin/opencode"
    opencode_timeout_seconds: int = 900
    workspace: str = "/workspace"
    master_url: str
    report_secret: SecretStr


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached instance of the master settings."""
    return Settings()
