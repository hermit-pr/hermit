# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://gitlab.com/hermit-bot/hermit/-/compare/v0.1.0...main
[0.1.0]: https://gitlab.com/hermit-bot/hermit/-/tags/v0.1.0