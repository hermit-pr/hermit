# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Initial development of H.E.R.M.I.T, the airgapped code-review bot for on-premise
GitLab and GitHub. Not released yet — will be validated against a second lab
environment before tagging `v0.1.0`.

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
  `HERMIT_REPORT_SIGNING_KEY`), so replicas can be scaled up and restarted
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

### Fixed

- GitHub `@hermit` comment authorization no longer re-reads the consumed
  request stream; it uses the already-parsed payload.
- GitLab webhook requests are no longer double-counted against the rate limiter.