"""Application settings, loaded from environment variables.

H.E.R.M.I.T reads its configuration from environment variables prefixed with
``HERMIT_``. A local ``.env`` file is also supported for development.
"""

import json
import os
import shlex
from functools import lru_cache
from typing import List, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GitProvider = Literal["github", "gitlab"]

DEFAULT_REVIEW_RULES = (
    "You are a code reviewer. Answer with exactly ONE JSON code block and "
    "nothing else: no reasoning, no commentary.\n\n"
    "The complete diff of the change under review is in the user message, "
    "between the BEGIN and END markers. It is the only source of truth for "
    "what changed.\n\n"
    "The diff is not the repository\n"
    "- It lists only the files that changed. Files that use a changed name but "
    "were not edited do not appear in it.\n"
    "- The checkout holds the whole repository at the pull-request head: use "
    "it to find what else points at a name this diff renames or removes.\n"
    "- Never conclude from the diff that a call site is the only one.\n\n"
    "Scope — what you may report\n"
    "- A finding is valid only when it is CAUSED by a line added or removed by "
    "this diff; `impact` states that causal link in one sentence.\n"
    "- A small diff can break code elsewhere: when a changed line renames, "
    "removes, or changes the contract of something used in another file, "
    "report it — anchored on the changed line, naming the affected code in "
    "`impact`.\n"
    "- Do NOT report: pre-existing bugs in code this diff does not touch, "
    "unrelated files, style of untouched code, missing tests unrelated to the "
    "change, hypothetical future problems, or 'consider also' suggestions.\n"
    "- When in doubt about a finding this diff did not cause, drop it. Noise "
    "is reporting what this diff did not cause; silence about what it broke is "
    "worse. An approval means: I checked every name this diff changes, and "
    "nothing outside the diff still uses the old one.\n\n"
    "Verification — required, not optional\n"
    "- The diff is already provided: never run `git diff` to rebuild it.\n"
    "- For every name this diff removes, renames, or changes the signature of "
    "(function, class, constant, import, configuration key), run one `grep` "
    "for the OLD name before answering. `grep` covers the whole checkout, so a "
    "hit is a reference this diff did not update; read the hit to confirm.\n"
    "- Do not browse anything no changed name reaches. `git show "
    "target-branch:<path>` shows a file as it was on the target branch.\n\n"
    "Before you answer, check\n"
    "1. Which names does this diff remove, rename, or change the contract of?\n"
    "2. For each one, what did `grep` return outside the files the diff "
    "changes?\n"
    "3. Which of those hits does this diff already update?\n"
    "Any hit left un-updated is a `medium` finding; answering `Approve` with "
    "one is wrong.\n\n"
    "Findings must be anchored\n"
    "- `anchor`: an exact quote (at most 120 characters) of one added or "
    "removed line of the diff — the line that CAUSES the impact. Findings "
    "whose anchor is not in the diff are discarded automatically, so copy the "
    "line verbatim.\n"
    "- When the impact is in another file, the anchor is still the changed "
    "line of the diff (the renamed `def`, the removed import, the new "
    "signature), never the stale reference in the affected file.\n"
    "- `file` and `line`: where the impact is. They usually differ from the "
    "file of the anchor.\n"
    "- If you cannot quote a diff line that causes the impact, the finding is "
    "out of scope: drop it.\n\n"
    "Example — the diff renames `parse_config` to `load_config` in config.py "
    "but worker.py is not updated. `grep parse_config` returns worker.py, "
    "which this diff does not update:\n"
    '  {"title": "worker.py still imports parse_config", "file": "worker.py", '
    '"line": 3, "anchor": "def load_config(path):", "impact": "worker.py '
    'imports parse_config, which no longer exists", "detail": "...", '
    '"fix": "update the import and the call in worker.py"}\n'
    "  `anchor` quotes the changed line in config.py; `file` names where the "
    "damage is.\n\n"
    "Severity\n"
    "- critical: unsafe as merged — security hole, crash, data loss, broken "
    "build.\n"
    "- medium: incomplete or inconsistent change: a caller, handler, or "
    "reference the diff failed to update (including in a file the diff does "
    "not change), a missed edge case with real impact.\n"
    "- low: an inconsistency with project conventions introduced by this "
    "diff, with no functional impact.\n"
    "- Do not inflate severity; drop anything the diff did not cause.\n\n"
    "Output format — exactly one JSON block:\n"
    "```json\n"
    "{\n"
    '  "summary": "one or two sentences: what this change does and whether it '
    'is safe",\n'
    '  "critical": [\n'
    '    {"title": "brief summary", "file": "path/to/file", "line": 12, '
    '"anchor": "exact added or removed line", "impact": "why this change '
    'breaks X", "detail": "concise explanation", "fix": "concrete fix"}\n'
    "  ],\n"
    '  "medium": [],\n'
    '  "low": [],\n'
    '  "verdict": "Approve"\n'
    "}\n"
    "```\n\n"
    "Rules:\n"
    "- `summary` is mandatory, even for an approval: name the files or the "
    "behavior that changed.\n"
    "- Omit empty sections or use empty arrays.\n"
    '- `verdict` is "Request changes" when any critical or medium finding '
    'exists, otherwise "Approve".\n'
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
    git_read_token: SecretStr | None = None

    github_token_map: dict[str, str] = Field(default_factory=dict)
    git_read_token_map: dict[str, str] = Field(default_factory=dict)

    webhook_secret: SecretStr = Field(..., min_length=16)
    job_id_signing_key: SecretStr | None = None
    vllm_endpoint: str = Field(..., description="URL of the vLLM inference endpoint")
    model: str = Field(..., description="Model served by the vLLM endpoint")
    review_rules: str = ""
    vllm_api_key: SecretStr | None = None
    policy_file_path: str = "AGENTS.md"
    trigger_tags: List[str] = Field(default_factory=lambda: ["@hermit", "/recheck"])
    require_commenter_membership: bool = True

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
    opencode_init_image: str = "ghcr.io/anomalyco/opencode:2.1.4"
    opencode_init_bin_path: str = "/usr/local/bin/opencode"
    opencode_init_image_pull_policy: str = "IfNotPresent"
    opencode_init_resources: dict = Field(
        default_factory=lambda: {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        }
    )
    opencode_timeout_seconds: int = 900
    max_prompt_diff_bytes: int = 65536
    review_max_steps: int = 12
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

    @field_validator("github_token", "gitlab_token", "git_read_token", mode="after")
    @classmethod
    def _coerce_empty_secretstr(cls, value: SecretStr | None) -> SecretStr | None:
        """Treat an empty SecretStr as None (secretKeyRef may inject an empty value)."""
        if value is not None and not value.get_secret_value():
            return None
        return value

    @model_validator(mode="after")
    def _collect_org_tokens(self) -> "Settings":
        """Collect ``HERMIT_*_ORG_<N>_NAME/TOKEN`` env vars into token maps.

        When the Helm chart injects per-org tokens via ``secretKeyRef`` they
        land as numbered environment variables (the index is assigned by a
        ``range`` loop in the deployment template).  This validator scans the
        process environment, pairs each ``_NAME`` with its ``_TOKEN``, and
        merges them into ``github_token_map`` / ``git_read_token_map``.
        """
        rw_orgs: dict[str, str] = {}
        read_orgs: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("HERMIT_GITHUB_RW_ORG_") and key.endswith("_NAME"):
                idx = key[len("HERMIT_GITHUB_RW_ORG_") : -len("_NAME")]
                token_key = f"HERMIT_GITHUB_RW_ORG_{idx}_TOKEN"
                if token_key in os.environ:
                    rw_orgs[value] = os.environ[token_key]
            if key.startswith("HERMIT_GIT_READ_ORG_") and key.endswith("_NAME"):
                idx = key[len("HERMIT_GIT_READ_ORG_") : -len("_NAME")]
                token_key = f"HERMIT_GIT_READ_ORG_{idx}_TOKEN"
                if token_key in os.environ:
                    read_orgs[value] = os.environ[token_key]
        if rw_orgs:
            self.github_token_map = {**rw_orgs, **self.github_token_map}
        if read_orgs:
            self.git_read_token_map = {**read_orgs, **self.git_read_token_map}
        return self

    @model_validator(mode="after")
    def _validate_tokens(self) -> "Settings":
        """Fail fast when the token for the configured provider is missing."""
        if self.git_provider == "github":
            if not self.github_token and not self.github_token_map:
                raise ValueError(
                    "HERMIT_GITHUB_TOKEN or HERMIT_GITHUB_TOKEN_MAP is required "
                    "when git_provider is 'github'"
                )
            if not self.git_read_token and not self.git_read_token_map:
                raise ValueError(
                    "HERMIT_GIT_READ_TOKEN or HERMIT_GIT_READ_TOKEN_MAP is "
                    "required when git_provider is 'github'"
                )
        if self.git_provider == "gitlab":
            if not self.gitlab_token:
                raise ValueError(
                    "HERMIT_GITLAB_TOKEN is required when git_provider is 'gitlab'"
                )
            if not self.git_read_token:
                raise ValueError(
                    "HERMIT_GIT_READ_TOKEN is required when git_provider is 'gitlab'"
                )
        return self

    @field_validator("github_token_map", "git_read_token_map", mode="before")
    @classmethod
    def _parse_token_map(cls, value: object) -> dict[str, str]:
        """Parse a JSON string token map from an environment variable."""
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                return dict(json.loads(value))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid JSON in token map: {exc}") from exc
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        return {}


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
    opencode_init_image: str = "ghcr.io/anomalyco/opencode:2.1.4"
    opencode_init_bin_path: str = "/usr/local/bin/opencode"
    opencode_timeout_seconds: int = 900
    max_prompt_diff_bytes: int = 65536
    review_max_steps: int = 12
    workspace: str = "/workspace"
    master_url: str
    report_secret: SecretStr


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached instance of the master settings."""
    return Settings()
