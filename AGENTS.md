# AGENTS.md

## Project state

H.E.R.M.I.T (**Hermit Emits Reviews for Merge-requests In Total-isolation**) is a configurable code-review bot for **on-premise GitLab & GitHub**. It publishes a review on every PR/MR a webhook is configured for, via the opencode agent against a dedicated self-hosted **vLLM** endpoint.

The bot is implemented in **Python** (FastAPI webhook server). `README.md` is the spec of intended behavior; treat it as the contract. Do not invent CLI flags, config keys, or commands that aren't in it.

## Layout

- `src/hermit/` — the bot: `server.py` (FastAPI app, webhook endpoints), `config.py` (env-driven settings), `k8s.py` (reviewer pod spawning with optional init container for opencode), `slave.py` (reviewer pod entrypoint), `providers/` (GitHub/GitLab API clients), `signing.py` (webhook auth).
- `tests/` — pytest suite (async tests via pytest-asyncio, mock transports for HTTP).
- `docker/` — container image: `Dockerfile` + `entrypoint.sh` (shfmt `-i2` + shellcheck clean).
- `helm/hermit/` — Helm chart to deploy the bot (ConfigMap for non-secret config, Secret for tokens/webhook secret).

## Commands

All dev tooling runs from a `venv` + `pip install -e '.[dev]'`.

- Format: `black .` then `isort .`; shell scripts via `docker run --rm -v "$PWD:/workspace" -w /workspace alpine:latest sh -c "apk add --no-cache shfmt >/dev/null && shfmt -i 2 -w docker/entrypoint.sh"`
- Lint (must all pass, zero warnings): `black --check . && isort --check-only . && flake8 src tests && pylint src tests && docker run --rm -v "$PWD:/workspace" -w /workspace koalaman/shellcheck-alpine:stable shellcheck docker/entrypoint.sh`
- Tests: `pytest -v`
- Image: `docker build -f docker/Dockerfile -t hermit .`
- Chart (run in the CI image so the local `helm` doesn't need installing):
  - `docker run --rm -v "$PWD:/workspace" -w /workspace alpine/helm:3.21.1 lint helm/hermit`
  - `docker run --rm -v "$PWD:/workspace" -w /workspace alpine/helm:3.21.1 template hermit helm/hermit`

## Conventions

- Every Python function/class/module needs a docstring — pylint `C0116` etc. are enforced, so tests too.
- flake8 uses `.flake8` (its 7.x cannot read `pyproject.toml`); black/isort/pylint config lives in `pyproject.toml`.
- Line length 88 for Python; shell scripts use 2-space indentation (`shfmt -i 2`).

## CI constraints

- GitLab CI only. Every push runs **Secret Detection** (enabled) + SAST, plus lint/test/shellcheck/shfmt/helm jobs. Never commit real tokens/keys; scraped secrets fail the pipeline. Chart `secrets` values must stay empty in the repo.
- `docker-build` job only runs when `$CI_REGISTRY_IMAGE` is set. It builds `docker/Dockerfile` and pushes to `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA`.
- `helm-package` job packages the Helm chart and pushes to `oci://$CI_REGISTRY_IMAGE/charts`.

## Code Style & Linter Policy

### The Pylint Doctrine

You are strictly monitored by Pylint. You must output clean code, but pragmatism is allowed for complex orchestrations.

**ALLOWED INLINE DISABLES** (use `# pylint: disable=...` inline only):

- `W0613 (unused-argument)`: ONLY allowed for FastAPI route signatures and GitHub webhook payload receivers.
- `R0903 (too-few-public-methods)`: Allowed for Pydantic models, Dataclasses, and config objects.
- `R0913 (too-many-arguments)` & `R0917 (too-many-positional-arguments)`: Allowed ONLY when instantiating official Kubernetes API objects (e.g., `V1Pod`, `V1Job`).
- `W0511 (fixme)`: Allowed. Do not block CI on `# TODO` or `# FIXME`.
- `W0718 (broad-exception-caught)`: Allowed ONLY at the very top of FastAPI entrypoints (to return generic 500s) or within the outermost loop of a K8s watcher. For all other logic, catch specific exceptions.
- `C0415 (import-outside-toplevel)`: Tolerated ONLY to prevent circular dependencies in FastAPI routers/schemas. Do not abuse it.
- `R0914 (too-many-locals)`, `R0915 (too-many-statements)`, `R0911 (too-many-return-statements)`: Tolerated ONLY in the main Webhook payload parsing function, where flattening the complex JSON inevitably creates large structural blocks.
- `W0212 (protected-access)`: Tolerated ONLY if absolutely necessary to interface with undocumented behaviors of the `kubernetes-client`.

**STRICTLY FORBIDDEN** (code will be rejected):

- `W0611 (unused-import)`: NEVER leave dead imports.
- `W0719 (broad-exception-raised)`: NEVER `raise Exception("...")`. You MUST use specific, typed exceptions.
- `E*` (All fatal errors): Zero tolerance for undefined variables, missing members, or syntax errors.

### Kubernetes & FastAPI Constraints

- **Absolute Typing:** Type hints are MANDATORY for all function signatures and return types.
- **K8s API Resilience:** Whenever you call the Kubernetes API, you MUST wrap it in a `try/except` block specifically targeting `ApiException`. You must handle `404 Not Found` gracefully (it is a valid state, not a crash).
- **No Silent Failures:** If a pod state update fails, log it via a structured logger. Do not use standard `print()`.

### Airgap & Security Compliance (Zero Trust)

- **Smart Defaults for URLs:** GitHub Enterprise URLs, Kubernetes master IPs, or external endpoints MUST be overwritable from Environment Variables (e.g., use `os.getenv("GITHUB_URL", "https://api.github.com")`). Do not force env vars for standard public endpoints if a sane default exists.
- **TLS Verification:** Whenever using `requests` or `httpx`, you must account for custom CA certificates. The SSL verify path MUST be configurable via an environment variable (e.g., `REQUESTS_CA_BUNDLE` or a custom setting, falling back to `True`).

## Integration notes (context for the harness)

- Integration points: GitLab/GitHub **webhook** (POST into the bot), **opencode** agent as the review driver (run as a subprocess in a workspace dir), **vLLM HTTP endpoint** for inference, and the Git host API to fetch diffs and post reviews.
- Bot authenticates to the Git host with scoped tokens; inbound webhooks must be validated against a configured secret (GitHub HMAC `X-Hub-Signature-256`, GitLab `X-Gitlab-Token`).
- Airgapped private PKI: the Helm chart (`caBundle` values) can inject a private root CA via an existing ConfigMap/Secret into the master and every reviewer pod (`k8s.py` mounts a `ca-bundle` volume and sets `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`GIT_SSL_CAINFO`/`CURL_CA_BUNDLE`/`NODE_EXTRA_CA_CERTS` from `HERMIT_CA_BUNDLE_PATH`); the CA content itself is never stored in the chart.