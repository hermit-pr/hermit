"""Application settings, loaded from environment variables.

H.E.R.M.I.T reads its configuration from environment variables prefixed with
``HERMIT_``. A local ``.env`` file is also supported for development.
"""

import shlex
from functools import lru_cache
from typing import List, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GitProvider = Literal["github", "gitlab"]

DEFAULT_REVIEW_RULES = (
    "Write the review as a single markdown comment with these four sections, "
    "in this order:\n"
    "- ## Critical changes that need to be fixed\n"
    "- ## Medium issues\n"
    "- ## Low issues\n"
    "- ## General feedback\n"
    "Put bugs, security holes and crashes under critical; logic problems and "
    "missing edge cases under medium; style and minor refactors under low. "
    "Be concise and concrete."
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
    review_rules: str = DEFAULT_REVIEW_RULES
    vllm_api_key: SecretStr | None = None
    policy_file_path: str = "AGENTS.md"

    rate_limit_per_ip: int = 60
    rate_limit_global: int = 600
    rate_limit_window_seconds: int = 60
    max_body_bytes: int = 10 * 1024 * 1024

    pod_cpu_request: str = "200m"
    pod_memory_request: str = "512Mi"
    pod_cpu_limit: str = "1"
    pod_memory_limit: str = "2Gi"

    opencode_bin: str = "opencode"
    opencode_args: List[str] = Field(default_factory=lambda: ["run"])
    opencode_init_image: str = "ghcr.io/anomalyco/opencode:latest"
    opencode_init_bin_path: str = "/usr/local/bin/opencode"
    workspace: str = "/workspace"

    master_url: str = Field(..., description="URL reviewer pods use to report back")
    pod_image: str = Field(..., description="Image to run reviewer pods")
    pod_namespace: str = ""
    pod_service_account: str = ""
    report_timeout_seconds: int = 1800
    pod_spawner: Literal["k8s", "fake"] = "k8s"
    kube_config: str | None = None

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"


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
    review_rules: str = DEFAULT_REVIEW_RULES
    vllm_api_key: SecretStr | None = None
    policy_file_path: str = "AGENTS.md"
    opencode_bin: str = "opencode"
    opencode_args: List[str] = Field(default_factory=lambda: ["run"])
    opencode_init_image: str = "ghcr.io/anomalyco/opencode:latest"
    opencode_init_bin_path: str = "/usr/local/bin/opencode"
    workspace: str = "/workspace"
    master_url: str
    report_secret: SecretStr


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached instance of the master settings."""
    return Settings()
