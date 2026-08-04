# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-04

Initial release of H.E.R.M.I.T, the airgapped code-review bot for on-premise
GitLab and GitHub.

### Added

- Webhook-driven reviews published on every PR/MR, plus on-demand `@hermit` comments.
- Master webhook server (FastAPI) for GitHub and GitLab, with HMAC / token validation.
- Reviewer (slave) pods that clone the repository, compute the diff, run opencode
  against a self-hosted vLLM endpoint, and report the review back to the master.
- OpenCode integration with a generated `opencode.json`, including an optional
  vLLM API key passed via the per-job Kubernetes Secret.
- Kubernetes spawner with a per-job Secret and a read-only init container for opencode.
- Helm chart with ConfigMap, RBAC, ServiceAccount and Secret references.
- Full lifecycle logging on both the master and the slave pods; secrets are never logged.
- Version surfaced via `/healthz`, `HERMIT_VERSION` in pods, Kubernetes labels and the Helm chart.
- `CHANGELOG.md`; versioned container images and OCI Helm chart published by GitLab CI.