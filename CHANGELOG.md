# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- ``require_commenter_membership`` setting (env ``HERMIT_REQUIRE_COMMENTER_MEMBERSHIP``,
  default ``true``). When ``false``, ``@hermit`` comments from non-members are
  allowed to trigger reviews, bypassing the org/group membership check.
- Multi-org token support for GitHub Fine-Grained PATs: ``HERMIT_GITHUB_TOKEN_MAP``
  and ``HERMIT_GIT_READ_TOKEN_MAP`` (JSON ``{"org": "pat", ...}``) resolve the
  correct PAT per organisation. The existing ``HERMIT_GITHUB_TOKEN`` and
  ``HERMIT_GIT_READ_TOKEN`` act as fallbacks for unmapped orgs. Helm chart
  exposes these via ``githubRwToken`` / ``gitReadToken`` blocks with ``token``
  and ``orgs`` keys.

### Fixed

- Reviewer pod failures (e.g. wrong token, clone errors) no longer leave a
  perpetual ``pending`` commit status on the PR. The master now sets an
  ``error`` commit status when the pod enters ``Failed`` phase, and the
  slave proactively reports failures on unhandled exceptions before exiting.
- Durable secret for ``Failed`` reviewer pods is no longer deleted by the
  sweeper, preserving the ``failed`` annotation so watchers can discover
  the failure and report it to the PR.

### Changed

- Review prompt redesigned to reduce noise: sections with no findings are now
  skipped entirely instead of being filled with ``None.``. A mandatory
  ``## Verdict`` section (Approve / Request changes) replaces the
  ``## General feedback`` section. The agent is explicitly told not to
  comment on code that is already correct or handled properly.
- Policy compliance added as an explicit review criterion: every change is
  audited against the project ``AGENTS.md`` from the base commit.
- Codebase inspection scope narrowed from "inspect the full codebase" to
  "inspect related files when the diff suggests they may need updating".

## [0.2.8] - 2026-08-06

### Added

- Completed job retention: the master no longer deletes reviewer pods
  immediately after posting a review. Pods stay for 15 minutes (configurable
  via ``COMPLETED_RETENTION``), then a background GC task running every 5
  minutes cleans them up. This gives operators time to inspect pod logs for
  debugging.
- OpenCode internal log file (``~/.local/share/opencode/log/opencode.log``)
  is read and logged to the pod's stdout after the process exits, providing
  visibility even when ``--print-logs`` does not redirect to stderr.
- Raw NDJSON output from opencode is logged to the pod's stdout before
  parsing, giving full visibility into the model's output stream.
- ``: Refactored ``DEFAULT_REVIEW_RULES`` to use a fill-in template format
  with explicit section placeholders and clear instructions to suppress
  preamble and meta-commentary.

### Changed

- Job status renamed from "posted" to "completed" everywhere (Kubernetes
  annotation, durable secret, in-memory status, tests).
- ``_watch_job()`` no longer deletes the pod in its ``finally`` block; only
  removes the job from the in-memory store.

### Fixed

- DeepSeek reasoning/thinking blocks (``<thinking>...</thinking>``) are now
  stripped from the extracted review text via a regex substitution in
  ``extract_text()``. This prevents thinking content from being posted as
  part of the review on on-premise vLLM/LiteLLM deployments.
- Conversational preamble ("Let me check...", "Looking at this PR...") is
  stripped from the review body before posting. The output is truncated to
  the first ``## `` heading line, removing all preliminary meta-commentary.

## [0.2.7] - 2026-08-06

### Fixed

- Signal handler noise on successful review: the slave pod's SIGTERM handler
  no longer POSTs a spurious failure report when the review was already sent.
  Previously the master deleted the pod (via ``cleanup()``) immediately after
  posting the review, causing a sequence of "received signal 15, reporting
  failure" → "report for unknown job → 404" in both the slave and master logs.
- Master log ordering: "received review from slave" now appears before
  ``cleanup()``, so the log order reflects the actual event sequence.

### Added

- ``opencode run`` now uses ``--print-logs`` flag so opencode's internal logs
  are emitted on stderr and captured by the pod's log collector. Previously
  these were written to ``/home/hermit/.local/share/opencode/log/`` and lost
  when the pod was deleted.

## [0.2.6] - 2026-08-06

### Fixed

- Reviewer pod stalled because bash permissions had ``"*": "ask"``, which
  deadlocked when opencode's built-in ``explore`` subagent tried to run
  diagnostic commands like ``which rg``. The ``--auto`` flag should have
  auto-approved asks but did not apply at the agent level.
- ``*: deny`` is now the default for bash permissions. An explicit allow-list
  gates every permitted command: git read-only operations, grep, find, cat,
  head, tail, ls, wc, sort, uniq, cut, tr, echo, which, pwd, env, date,
  printf, expr, test, true, false, dirname, basename, xargs, and read. Any
  command not in the allow-list (rm, curl, apt, docker, kubectl, chmod, tee,
  mv, mkdir, cp, kill, sudo, mount, etc.) is blocked.
- Agent ``task`` permission set to ``{"*": "deny"}`` to prevent the
  hermit-reviewer agent from spawning subagents (explore, general, scout)
  entirely — their permission prompts were the root cause of the stalls.

### Added

- NetworkPolicy template (``slave-networkpolicy.yaml``) to restrict egress
  from reviewer pods. When enabled via ``networkPolicy.enabled: true``,
  reviewer pods can only reach: kube-dns (UDP/TCP 53), the configured git
  host CIDR and port, the configured vLLM endpoint CIDR and port, and the
  master pod in the same namespace. This prevents the opencode agent from
  making outbound connections to any other destination, even if an allowed
  bash command (e.g. ``curl``) were never explicitly denied in future code.

## [0.2.5] - 2026-08-05

### Fixed

- Reviewer pod stalled indefinitely because ``--agent hermit-reviewer`` CLI
  flag interacted badly with opencode's ``run`` mode. The agent is now
  activated via the ``default_agent`` config key in ``opencode.json``
  instead of the CLI flag. The ``default_agent`` key applies across all
  opencode interfaces including ``run``, TUI, and GitHub Actions.

## [0.2.4] - 2026-08-05

### Fixed

- ``hermit-reviewer`` agent stalled indefinitely on review tasks. Root cause
  was ``mode: all`` (the agent wasn't primary) combined with
  ``question: deny`` blocking the model from asking for clarification. Changed
  to ``mode: primary`` and added ``recovery: deny`` to the agent's permission
  block to prevent opencode from injecting stuck-detection prompts.

## [0.2.3] - 2026-08-05

### Fixed

- ``hermit-reviewer`` agent used ``mode: subagent`` which prevented it from
  being invoked directly via ``--agent``; changed to ``mode: all`` so opencode
  could run it. (Later found ``primary`` was the correct mode.)

## [0.2.2] - 2026-08-05

### Changed

- Review architecture redesigned around opencode agents: review rules, output
  format, and evaluation criteria move from the user prompt into a dedicated
  ``hermit-reviewer`` subagent defined in the generated ``opencode.json``
  config. The agent's system prompt contains the trusted rules; the user prompt
  carries only PR context data (title, description, diff, secret scan).
- ``opencode`` invoked with ``--agent hermit-reviewer`` so the model receives a
  single, consistent system-level identity instead of the conflicting "You are
  H.E.R.M.I.T" persona that caused it to treat the prompt as a template
  document rather than a review task.
- Prompt file stripped to pure data — no review rules, no output format
  instructions, no identity declaration. This eliminates the "would you like me
  to execute this template?" meta-response observed with earlier prompt formats.
- ``OpenCodeRunner`` accepts an optional ``review_rules`` parameter and merges
  it with ``DEFAULT_REVIEW_RULES`` via a newline separator in the agent prompt.
- ``DEFAULT_REVIEW_RULES`` no longer neutralized in the prompt — backticks and
  markdown formatting are preserved because they live in the trusted system
  prompt.

### Removed

- Two tests removed: ``test_build_review_prompt_always_includes_default_rules``
  and ``test_build_review_prompt_neutralizes_custom_rules`` (rules are no longer
  in the user prompt at all).

## [0.2.1] - 2026-08-05

### Added

- ``/recheck`` comment trigger supported alongside ``@hermit``. Any comment
  containing ``/recheck`` (case-insensitive) now spawns a review. The comment
  trigger list is configurable via ``HERMIT_TRIGGER_TAGS`` (JSON list in
  ConfigMap, Helm ``config.triggerTags``). Default: ``["@hermit", "/recheck"]``.

## [0.2.0] - 2026-08-05

First stable release — the reviewer pipeline now works end-to-end in
production environments (airgapped GitHub Enterprise + self-hosted vLLM).

### Changed

- Docker image switched from `python:3.13-slim` (glibc) to `python:3.14-alpine`
  (musl) for compatibility with the opencode init-container binary; added
  ``libstdc++`` and ``libgcc`` packages for C++ runtime support (image size
  reduced from ~120 MB to ~51 MB).
- OpenCode permission model rewritten for opencode ≥1.18.10: per-tool rules
  replace the legacy flat deny/allow arrays; ``edit``, ``webfetch``, and
  ``question`` tools are denied; bash commands use per-pattern rules (read
  commands allowed, write commands denied, everything else ``ask``); all
  non-vLLM providers explicitly disabled.
- ``opencode_args`` config key removed; arguments ``["run", "--auto",
  "--format", "json"]`` are now hardcoded in ``OpenCodeRunner`` — the NDJSON
  output format is required by the parser and there is no use-case for changing
  it.
- Reviewer pod entrypoint changed from ``python -m hermit.slave`` to the
  ``hermit-slave`` console script.
- The model name from ``HERMIT_MODEL`` is passed verbatim into the opencode
  config with no transformation; the operator controls the exact name the API
  sees.
- Uvicorn access and error logs now share the same ISO-8601 timestamp format as
  the application logger.

### Fixed

- Environment variable prefix typo: ``HERMIT_OPCODE_*`` → ``HERMIT_OPENCODE_*``
  in the Helm ConfigMap template and ``k8s.py`` pod environment. The
  misspelling caused every setting to silently fall back to its Python default
  instead of reading the chart-configured values (e.g. init-container image,
  timeout).
- GitHub Enterprise org membership checks now accept HTTP ``204 No Content``
  (GHE returns this for confirmed members, not ``200 OK``). Previously only
  ``200`` was treated as success, causing false-negative membership rejections.
- ``python -m hermit.slave`` did nothing because ``slave.py`` had no
  ``if __name__ == "__main__"`` guard — the module imported and exited with
  code 0 without running the review. Added the guard and switched to the
  console-script entrypoint.
- ``$HOME`` was unset in reviewer pods, causing opencode's Bun runtime to
  attempt writes to ``/.local/`` on the read-only root filesystem. Added
  ``HOME=/home/hermit`` to point at the writable volume mount.
- ``opencode_args`` JSON serialization: ``k8s.py`` used ``shlex.join()`` on the
  arg list, producing a plain string that pydantic-settings rejected as
  invalid JSON. Args are no longer serialized (removed from ConfigMap).
- Docker image added ``libstdc++`` and ``libgcc`` so the musl-compiled opencode
  binary from the init container can resolve C++ symbols (was ``/opencode-bin/
  opencode: not found`` on glibc).

## [0.1.1] - 2026-08-05

### Fixed

- GitHub Enterprise membership and collaborator checks returning false negatives
  (403 Forbidden for all `@hermit` comments). The root cause was
  `follow_redirects=True` in the `GitClient` base class, which caused `httpx` to
  silently follow 302 redirects from GHE's `/orgs/<org>/members/<user>` and
  `/repos/<owner>/<repo>/collaborators/<user>` endpoints to `/login`, consuming
  the redirect before the explicit 302/403 handling in `github.py` could
  interpret it. The `GitClient` now creates the `AsyncClient` with
  `follow_redirects=False`, restoring the expected behaviour.

## [0.1.0] - 2026-08-04

First release of H.E.R.M.I.T, the airgapped code-review bot for on-premise
GitLab and GitHub.

### Added

- Webhook-driven reviews published on every PR/MR, plus on-demand `@hermit` comments.
- Master webhook server (FastAPI) for GitHub and GitLab, with HMAC / token validation.
- Reviewer (slave) pods that clone the repository, compute the diff, run opencode
  against a self-hosted vLLM endpoint, and report the review back to the master.
- OpenCode integration with a generated `opencode.json`, including an optional
  vLLM API key passed via the per-job Kubernetes Secret.
- Kubernetes spawner with a per-job Secret and a read-only init container for opencode.
- Helm chart with ConfigMap, RBAC, ServiceAccount and Secret references, plus
  optional **Ingress** (`ingress.yaml`) support.
- Full lifecycle logging on both the master and the slave pods; secrets are never logged.
- Version surfaced via `/healthz`, `HERMIT_VERSION` in pods, Kubernetes labels and the Helm chart.
- Pull requests from **GitHub forks** and **cross-project GitLab merge
  requests** are now supported; the review always reflects the PR/MR head (via
  `refs/pull/<n>/head` or the `source_repo` remote).
- Horizontal scaling: the master keeps durable job state in Kubernetes and
  derives **deterministic job ids** from the event (HMAC-SHA256 against
  `HERMIT_JOB_ID_SIGNING_KEY`), so replicas can be scaled up and restarted
  freely and duplicate webhook events collapse onto one job.
- Idempotent report handling: `/internal/report/<id>` is stateless and reads
  the durable job Secret; duplicate reports are ignored, and a review survives
  a master restart.
- Commit status API: pending/success/failure/error statuses are set on the PR
  head commit as the review progresses.
- `@hermit` comments from non-members on GitLab groups are rejected.
- Bound opencode subprocess runtime with a configurable timeout, and a
  concurrency cap that prevents resource exhaustion on webhook bursts.
- Startup fail-fast validation: a missing provider token aborts the process
  with a clear message; the version is logged at startup.
- Private CA support for airgapped deployments: a `caBundle` section injects an
  existing ConfigMap/Secret root CA into the master and every reviewer pod, and
  the bot sets the SSL/git/curl/node trust variables from it.
- GitHub Actions workflow mirroring the GitLab CI `docker-build`, publishing
  the image to GHCR on main pushes and version tags.
- PR title and body are passed to reviewers: `ChangeEvent` carries
  `pr_title`/`pr_body`, exported to reviewer pods as `HERMIT_PR_TITLE` /
  `HERMIT_PR_BODY`, and rendered as a neutralized `<pr_description>` section.
- Transient K8s API errors retried with exponential backoff in the watcher.
- Pod phase detection — reviewer pod crashes (OOM, ImagePullBackOff) detected
  immediately via `get_pod_phase`; full timeout no longer required.
- ASGI-level body size limiter (`_BodyLimitMiddleware`) handles chunked
  transfer encoding in addition to Content-Length.
- On startup, the master recovers watchers for running reviewer pods from a
  previous replica, using the remaining timeout from the original job.
- Reviewer pods get a separate, unprivileged ServiceAccount
  (`hermit-reviewer`); only the master Deployment retains pod/secret RBAC.
- Helm chart: master resource defaults (100m/128Mi requests, 500m/512Mi
  limits), probe timing parameters, and a startup probe.

### Changed

- Reviewer pods authenticate the clone with a `GIT_ASKPASS` helper instead of
  putting the token on the command line, so it never leaks into argv or logs.
- Reviewer pods hardened: `automountServiceAccountToken: false`, non-root
  init/openCode container (uid 1000), bounded `activeDeadlineSeconds`.
- Helm Role now allows `list`/`update` on Secrets, required for the durable job
  store.
- Review output instructions are additive only: the hardcoded default rules are
  always included and any configured `HERMIT_REVIEW_RULES` are appended; the
  option is now optional and never replaces the internal operating prompt.
- The PR/MR `ref` is rendered as `repo PR n` instead of a raw `#`.
- Posted reviews are prefixed with a bot/model header.
- The `report` endpoint performs the CAS (`mark_posted`) *before* posting the
  review, preventing duplicate review comments from race conditions.
- `HERMIT_GIT_HOST_URL` for GitHub Enterprise must include `/api/v3`, for
  self-hosted GitLab `/api/v4`; the clone URL automatically strips the API path.
- Helm `rbac.enabled: false` now correctly hides the Role and RoleBinding
  templates.

### Fixed

- GitHub `@hermit` comment authorization no longer re-reads the consumed
  request stream; it uses the already-parsed payload.
- GitLab webhook requests are no longer double-counted against the rate limiter.
- GitLab commit status used MR iid instead of numeric project id (silent 404).
- Watcher crash on transient K8s API errors (re-raise of non-404 `ApiException`).
- Duplicate webhook events overwrote in-flight jobs with a new report secret.
- `httpx.AsyncClient` now honors `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` for
  air-gapped GHES with private CA.
- `git fetch` / `git show` argument injection from webhook payload values.
- `opencode` subprocess killed on timeout instead of leaking a zombie.
- `git diff` output capped at 100 MB to prevent OOM in reviewer pods.
- K8s API calls use 30-second timeout to prevent thread pool exhaustion.
- `asyncio.get_event_loop()` replaced with `get_running_loop()`.
- `mark_posted` handles 404 gracefully on race with concurrent cleanup.
- `check_repo_collaborator` returns `False` on HTTP errors instead of raising.
- `get_pod_phase` handles `None` pod status gracefully.
- Durable state inconsistency when `post_review` fails after `mark_posted`.
- `HERMIT_OPCODE_ARGS` joined with `shlex.join` to preserve quoting.
- Sweep marks durable state `failed` before deleting secrets for Failed pods.
- `mark_failed` uses CAS (resource-version check) to prevent overwriting `posted` status.
- All remaining pylint `broad-exception-caught` replaced with specific exceptions.
- `k8s.py`, `jobs.py`, `slave.py` have zero pylint disables; `server.py` has only 2 justified background-loop exceptions.

[Unreleased]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.8...main
[0.2.8]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.7...v0.2.8
[0.2.7]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.6...v0.2.7
[0.2.6]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.5...v0.2.6
[0.2.5]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.4...v0.2.5
[0.2.4]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.3...v0.2.4
[0.2.3]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.2...v0.2.3
[0.2.2]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.1...v0.2.2
[0.2.1]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.2.0...v0.2.1
[0.2.0]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.1.1...v0.2.0
[0.1.1]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.1.0...v0.1.1
[0.1.0]: https://gitlab.com/hermit-bot/hermit/-/tags/v0.1.0