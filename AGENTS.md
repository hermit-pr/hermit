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

- Format: `black .` then `isort .`; also `shfmt -i 2 -w docker/entrypoint.sh`
- Lint (must all pass, zero warnings): `black --check . && isort --check-only . && flake8 src tests && pylint src tests`
- Tests: `pytest -v`
- Image: `docker build -f docker/Dockerfile -t hermit .`
- Chart: `helm lint helm/hermit` and `helm template hermit helm/hermit`

## Conventions

- Every Python function/class/module needs a docstring — pylint `C0116` etc. are enforced, so tests too.
- flake8 uses `.flake8` (its 7.x cannot read `pyproject.toml`); black/isort/pylint config lives in `pyproject.toml`.
- Line length 88 for Python; shell scripts use 2-space indentation (`shfmt -i 2`).

## CI constraints

- GitLab CI only. Every push runs **Secret Detection** (enabled) + SAST, plus lint/test/shellcheck/shfmt/helm jobs. Never commit real tokens/keys; scraped secrets fail the pipeline. Chart `secrets` values must stay empty in the repo.
- `docker-build` job only runs when `$CI_REGISTRY_IMAGE` is set. It builds `docker/Dockerfile` and pushes to `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA`.
- `helm-package` job packages the Helm chart and pushes to `oci://$CI_REGISTRY_IMAGE/charts`.

## Integration notes (context for the harness)

- Integration points: GitLab/GitHub **webhook** (POST into the bot), **opencode** agent as the review driver (run as a subprocess in a workspace dir), **vLLM HTTP endpoint** for inference, and the Git host API to fetch diffs and post reviews.
- Bot authenticates to the Git host with scoped tokens; inbound webhooks must be validated against a configured secret (GitHub HMAC `X-Hub-Signature-256`, GitLab `X-Gitlab-Token`).
- Airgapped private PKI: the Helm chart (`caBundle` values) can inject a private root CA via an existing ConfigMap/Secret into the master and every reviewer pod (`k8s.py` mounts a `ca-bundle` volume and sets `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`GIT_SSL_CAINFO`/`CURL_CA_BUNDLE`/`NODE_EXTRA_CA_CERTS` from `HERMIT_CA_BUNDLE_PATH`); the CA content itself is never stored in the chart.