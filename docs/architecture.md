# H.E.R.M.I.T Architecture

H.E.R.M.I.T is a configurable code-review bot for **on-premise GitLab and GitHub
instances**. It runs fully airgapped: model inference happens on a dedicated,
self-hosted **vLLM** endpoint, and review work is executed on the operator's
own Kubernetes cluster.

This document is the reference for the implementation. Treat it as the
contract; code must match it.

## Roles

Two processes share one container image:

| Role | Entrypoint | Lifetime | Responsibility |
| --- | --- | --- | --- |
| **master** | `python -m hermit.main` | long-running Deployment | receive webhooks, spawn a reviewer pod per PR/MR, collect the review, submit it to the PR/MR |
| **slave** (reviewer pod) | `python -m hermit.slave` | one-shot Pod, one per review | fetch the two branches, compute the diff, run **opencode** against **vLLM**, report the review back to the master |

External systems:

- **GitLab / GitHub instance** (on-premise) - source of webhooks, source of the
  code to review, target of the published review.
- **vLLM endpoint** (on-premise) - serves the model opencode uses for
  inference. Its configuration is outside the scope of H.E.R.M.I.T; H.E.R.M.I.T only needs
  the endpoint URL and the model name.
- **Kubernetes API** - the master creates and deletes reviewer pods (and their
  secrets) in its own namespace using the in-cluster client + RBAC.

## Review lifecycle

```
 Dev                                     Git host
  |  push + open PR / MR                    |
  +---------------------------------------->|            (0)
  |                                     webhook event     (1)
  |<=====================================================> master
  |                                         |
  |     master validates signature,         |
  |     builds ChangeEvent, creates a       |
  |     job with a per-review secret        |
  |                                         |
  |     master creates a Kubernetes Secret  (read-only git token + report secret)
  |     then a one-shot reviewer Pod        (2)
  |                                         v
  |                                   +-------------+
  |                                   | slave pod   |
  |                                   |-------------|
  |                                   | git fetch   |  (3a)
  |                                   | base..head  |
  |                                   | diff        |
  |                                   | opencode -> |  (3b)
  |                                   | vLLM        |
  |                                   | review text |
  |                                   +-----+-------+
  |                                         |
  |                         POST /internal/report/<id>  (4)
  |<=====================================================|
  |     master stores the review body,       |
  |     submits it to the PR/MR using the    |
  |     scoped write token                   (5)
  |---------------------------------------->|
  |     review comment published             |
```

### Step by step

0. A developer pushes a branch and opens a pull request / merge request on the
   on-premise Git host.
1. The webhook configured on the Git host posts the event to the master
   (`/webhook/github` or `/webhook/gitlab`).
2. The master validates the webhook (HMAC signature for GitHub, shared token
   header for GitLab). It normalizes the payload into a `ChangeEvent`, derives
   a **deterministic job id** (HMAC-SHA256 of the event against the shared
   signing key) plus a random one-time report secret, and creates a Kubernetes
   `Secret` holding the read-only git token, the report secret, and the event
   JSON, then creates a reviewer Pod in the master's namespace. The pod is
   non-root, uses `restartPolicy: Never`, disables service-account token
   auto-mounting, and only receives non-secret config plus references to the
   per-review Secret.
3. The slave pod:
   a. clones the repository with the read-only token — via `GIT_ASKPASS`, never
      on the command line — and diffs the base branch/commit against the head
      branch/commit. Pull requests from GitHub forks and cross-project GitLab
      merge requests are fetched through `refs/pull/<n>/head` or the
      `source_repo` remote; in all cases the code being reviewed is the PR/MR
      head, never the destination branch;
   b. runs `opencode` in the checked-out workspace against the on-premise
      vLLM endpoint, with the diff and the review rules in the prompt.
4. The slave POSTs the resulting review text to the master's internal endpoint
   `/internal/report/<job_id>`, authenticated with the per-review report
   secret (the master reconstructs the job from the durable Secret, so any
   replica can answer).
5. The master validates the report secret, then submits the review to the
   PR/MR using its scoped **write** token (GitHub pull-request review / GitLab
   merge-request note). The job is marked `posted` (idempotent: duplicate
   reports are ignored) and the master deletes the reviewer Pod and its
   Secret.

### Manual trigger from a PR/MR comment

H.E.R.M.I.T can also be asked to review on demand. When a user writes a comment
mentioning the bot (`@hermit`) on a pull/merge request, the comment event is
processed the same way:

1. GitHub `issue_comment` events on a pull request (or GitLab `note` events on
   a merge request) whose body contains `@hermit` are parsed into a
   `ChangeEvent` with only the repository and PR/MR number.
2. The master calls the Git host API (`resolve_refs`) to fill in the head and
   base refs/shas, then spawns a reviewer pod exactly as for a normal PR/MR
   event.
3. The resulting review is posted back to the PR/MR.

### Review output format

The prompt handed to opencode always asks for a single markdown review with
four explicit sections, in this order:

- **Critical changes that need to be fixed** - bugs, security holes, crashes;
- **Medium issues** - logic problems, missing edge cases, error handling;
- **Low issues** - style, naming, minor refactors;
- **General feedback** - overall assessment and suggestions.

The default `HERMIT_REVIEW_RULES` encodes this structure so every review,
automatic or manual, is formatted identically.

### H.E.R.M.I.T never merges or commits

H.E.R.M.I.T is strictly read-comment. It **never** merges, never commits, and never
pushes code. Its only write action is posting a review comment on a PR/MR
(GitHub pull-request review, GitLab merge-request note). No merge, commit or
push API is ever called.

## Security model

  - **H.E.R.M.I.T never merges or commits.** Its only write action is posting a review
  comment on a PR/MR. There is no merge, commit or push capability anywhere in
  the codebase.
- **Two tokens, separated privileges.** The *write* token lives only in the
  master (Kubernetes Secret mounted as env) and is the only credential that can
  publish reviews. The *read-only* token is created per review inside a
  job-scoped Kubernetes Secret and injected into the reviewer Pod, which is
  deleted when the job ends. The slave never receives write access.
- **Webhook authentication.** GitHub `X-Hub-Signature-256` (HMAC-SHA256 of the
  raw body) and GitLab `X-Gitlab-Token` are compared in constant time.
- **Report authentication.** The `/internal/report/<id>` endpoint requires a
  per-job secret (`X-Hermit-Report-Secret`), compared in constant time. The
  secret is random per job, generated at job-create time and stored in the
  durable job Secret; it is never reused across jobs.
- **No token in argv or logs.** Reviewer pods read the read-only git token
  through a `GIT_ASKPASS` helper injected as an environment variable; the
  clone command line never contains the token, so it never leaks into logs,
  pod spec, or command output.
- **Pod hardening.** Reviewer pods run as non-root (`runAsNonRoot`,
  `runAsUser/runAsGroup: 1000`), disable service-account token auto-mounting
  (`automountServiceAccountToken: false`), use `restartPolicy: Never` and a
  bounded `activeDeadlineSeconds`. RBAC is scoped to the master's own
  namespace: create/get/list/update/delete pods and secrets.
- **Airgap.** Everything runs on the operator's infrastructure; no data is sent
  outside the network.

## Data model

### `ChangeEvent`

Normalized webhook payload; same shape regardless of provider.

| Field | Meaning |
| --- | --- |
| `provider` | `github` or `gitlab` |
| `action` | webhook action that triggered the event |
| `repo` | `owner/name` (GitHub) or `namespace/project` (GitLab) |
| `ref` | PR number / MR IID |
| `head_sha`, `head_ref` | head commit / source branch |
| `base_sha`, `base_ref` | base commit / target branch |
| `pr_title`, `pr_body` | PR/MR title and description fed to the reviewer |
| `url`, `project_id` | display metadata |
| `source_repo` | source (fork) project path for cross-project GitLab MRs |

### `ReviewJob`

Record of one review. It is materialized in two places: a lightweight
in-memory handle kept by each master process, and a **durable Kubernetes
Secret** (`hermit.job=<id>`, annotation `hermit.dev/status`) that any master
replica can read to reconstruct the job after a restart or to deduplicate
reports.

| Field | Meaning |
| --- | --- |
| `id` | deterministic job id: HMAC-SHA256 of the event against the shared signing key |
| `event` | the originating `ChangeEvent` |
| `report_secret` | one-time secret for `/internal/report/<id>` |
| `status` | `pending` -> `reported` -> `posted` (or `failed`) |
| `pod_name`, `secret_name` | Kubernetes objects to clean up |
| `body` | review text reported by the slave |

### Horizontal scaling and idempotence

- **Deterministic job ids.** The job id is derived from the normalized event
  with HMAC-SHA256 using `HERMIT_REPORT_SIGNING_KEY` (defaults to the webhook
  secret). Every replica derives the same id for the same event, so a
  duplicated webhook (or a redelivery after a master restart) cannot spawn a
  second reviewer.
- **Dedup at the source of truth.** The durable job Secret is created with
  `Create` semantics; a conflicting `409` on the same `hermit.job` label means
  the event is already being handled and is dropped (`202`).
- **Restart survival.** A master that comes back or scales up simply reads the
  existing job Secret, restores the in-memory handle, and keeps waiting on the
  reporter or sweeps stale jobs. Nothing is lost when a master pod restarts.
- **Stateless report endpoint.** `/internal/report/<id>` is handled by reading
  the durable Secret, so the replica that receives the callback need not be the
  one that spawned the pod.
- **Polling watcher.** Masters do not watch the Kubernetes API for completion;
  they poll the durable Secret/status every `WATCH_POLL_SECONDS` (5s) and rely
  on the job being marked `posted` before cleanup.
- **Sweeper.** A background loop (every `SWEEP_INTERVAL_SECONDS`, 300s) deletes
  finished or timed-out reviewer pods and orphaned job Secrets, so a crashed
  master cannot leak pods or secrets.

## Configuration

All environment variables use the `HERMIT_` prefix. Secrets are handled as
`SecretStr` and never logged.

### Master

| Variable | Required | Meaning |
| --- | --- | --- |
| `HERMIT_GIT_PROVIDER` | yes | `github` or `gitlab` |
| `HERMIT_GIT_HOST_URL` | yes | on-premise Git host URL |
| `HERMIT_GITHUB_TOKEN` | for GitHub | write token (submits reviews) |
| `HERMIT_GITLAB_TOKEN` | for GitLab | write token (submits reviews) |
| `HERMIT_GIT_READ_TOKEN` | yes | read-only token handed to reviewer pods |
| `HERMIT_WEBHOOK_SECRET` | yes | validates inbound webhooks |
| `HERMIT_REPORT_SIGNING_KEY` | no | derives deterministic, horizontally-shared job ids (default: webhook secret) |
| `HERMIT_VLLM_ENDPOINT` | yes | passed to reviewer pods |
| `HERMIT_MODEL` | yes | passed to reviewer pods |
| `HERMIT_REVIEW_RULES` | no | passed to reviewer pods |
| `HERMIT_OPCODE_BIN` | no | opencode binary path inside pods |
| `HERMIT_OPCODE_ARGS` | no | whitespace-separated opencode args |
| `HERMIT_MASTER_URL` | yes | URL the pods use to reach `/internal/report` |
| `HERMIT_POD_IMAGE` | yes | container image for reviewer pods |
| `HERMIT_POD_NAMESPACE` | no | namespace for pods/secrets (in-cluster default) |
| `HERMIT_POD_SERVICE_ACCOUNT` | no | ServiceAccount for reviewer pods |
| `HERMIT_REPORT_TIMEOUT_SECONDS` | no | max wait for a review (default 1800) |
| `HERMIT_POD_SPAWNER` | no | `k8s` (default) or `fake` (local dev/tests) |
| `HERMIT_PORT`, `HERMIT_LOG_LEVEL`, `HERMIT_HOST` | no | HTTP server |

### Slave (set by the master on the pod)

| Variable | Meaning |
| --- | --- |
| `HERMIT_JOB_ID` | job id for the report callback |
| `HERMIT_GIT_PROVIDER`, `HERMIT_GIT_HOST_URL` | where to clone from |
| `HERMIT_GIT_READ_TOKEN` | read-only token (from the job Secret) |
| `HERMIT_REPO`, `HERMIT_HEAD_SHA/REF`, `HERMIT_BASE_SHA/REF` | what to diff |
| `HERMIT_SOURCE_REPO` | source (fork) project path for cross-project GitLab MRs |
| `HERMIT_VLLM_ENDPOINT`, `HERMIT_MODEL`, `HERMIT_REVIEW_RULES` | opencode config |
| `HERMIT_OPCODE_BIN`, `HERMIT_OPCODE_ARGS` | opencode invocation |
| `HERMIT_WORKSPACE` | working directory inside the pod |
| `HERMIT_MASTER_URL` | where to report the review |
| `HERMIT_REPORT_SECRET` | per-job report secret (from the job Secret) |

## Failure handling

- **Webhook invalid / unsupported event** - rejected with `401` or accepted
  and ignored (`202`), no pod is spawned.
- **Duplicate webhook event** - the deterministic job id collides on the
  durable Secret (`409`), so only the first copy runs; the others are dropped
  with `202`.
- **Master pod restarts / scales** - jobs are reconstructed from the durable
  Secrets, in-flight reviews continue, and the sweeper reclaims leftovers.
- **Pod fails to start or crashes** - the job is marked `failed`, the review is
  not published, and leftovers (pod/secret) are cleaned up.
- **Report timeout** - the job is marked `failed` and cleaned up.
- **Submit failure** - the master logs the error; the pod and its Secret are
  still cleaned up.

## Deployment

- One container image (`docker/Dockerfile`), two entrypoints
  (`docker/entrypoint.sh` for the master; the pod template runs
  `python -m hermit.slave`).
- Helm chart `helm/hermit`: master Deployment (replicaCount may be raised to
  scale horizontally — the `secrets` RBAC verbs and the durable job Secrets
  make this safe), Service, ConfigMap (non-secret config), Secret (write
  token, read token, webhook secret, optional report signing key),
  ServiceAccount + Role/RoleBinding for pod/secret management in the
  namespace.
