# Tempo

A Dockerized Python implementation of OpenAI's
[autonomous coding service specification](https://github.com/openai/symphony/blob/main/SPEC.md).
It continuously polls an issue tracker, creates an isolated workspace for each issue, and runs
Codex app-server sessions until the issue leaves an active state.

This port uses Django for the operator dashboard, JSON API, authenticated admin, and a durable
PostgreSQL execution kernel. Database claims, leases, heartbeats, checkpoints, retry timers, and
operator actions are authoritative; in-process state is a live cache of leased work.

## What is included

- Strict `WORKFLOW.md` YAML front matter and Jinja-compatible prompt rendering
- Dynamic workflow reload with last-known-good fallback
- GitHub Issues adapter and deterministic in-memory development adapter
- Global and per-state concurrency, priority ordering, claims, reconciliation, stall detection,
  continuation runs, and exponential retry backoff
- Collision-resistant, root-contained per-issue workspaces and all four lifecycle hooks
- Codex app-server JSONL client with thread/turn continuation, timeouts, token/rate-limit telemetry,
  approval/input handling, and host-side GitHub tool execution
- Project-native local validation with captured commands, exit codes, logs, cleanup, and a hard
  pull-request publication gate
- Per-run token, turn, validation-attempt, retry, and stall limits with durable safety stops
- Durable completion dispositions that prevent completed issues from being dispatched after restart
- Persistent Django models and authenticated admin views for issues, runs, sessions, validation
  attempts, and validation commands
- PostgreSQL-backed accepted work, retries, worker leases, heartbeats, idempotency keys, and
  checkpoints with SQLite retained for local development and deterministic tests
- Authenticated pause, resume, cancel, retry, requeue, unblock, reprioritize, feedback, and durable
  approval-inbox APIs with audited operator actions
- First-class organizations, projects, repositories, environments, workflow versions, credential
  references, quotas, and concurrent multi-workflow hosting
- Structured JSON logs, Django dashboard, health check, and REST status endpoints
- A non-root Docker image containing Python, Git, SSH, Node, and the Codex CLI
- Deterministic tests with fake tracker and app-server implementations

## Quick start

The checked-in workflow uses a GitHub tracker, so configure its repository token before startup:

```bash
cp .env.example .env
# Add GITHUB_TOKEN and either OPENAI_API_KEY or a persisted Codex login.
docker compose up --build
```

Open <http://localhost:8030> (or the `TEMPO_PORT` set in `.env`). Verify health with:

```bash
curl http://localhost:${TEMPO_PORT:-8030}/healthz
curl http://localhost:${TEMPO_PORT:-8030}/api/v1/state
```

The Compose service persists issue workspaces, runtime history, and Codex state in named volumes.
Stop it with `docker compose down`; add `-v` only when you intentionally want to delete those
volumes.

After changing Tempo, rebuild and restart every service with:

```bash
./scripts/restart-tempo.sh
```

The script preserves the workspace, database, and Codex home volumes, force-recreates the
containers, waits for their health checks, and prints the resulting service status.

## Django admin

Tempo applies database migrations automatically at startup. Create the first Django superuser with:

```bash
docker compose exec tempo python manage.py createsuperuser
```

Alternatively, set `TEMPO_ADMIN_USERNAME`, `TEMPO_ADMIN_PASSWORD`, and optionally
`TEMPO_ADMIN_EMAIL` in `.env` before the first startup; Tempo creates that account only when the
username does not already exist. The password is removed from Codex and validation environments.

Open `/admin/` for Django Admin. Runtime records are read-only there because editing them would not
change the authoritative tracker or live scheduler. The live operator screens remain available at
`/ops/` and `/ops/configuration/`.

## Configure GitHub Issues

Replace the `tracker` section in `WORKFLOW.md`:

```yaml
project:
  organization: your-org
  slug: your-project
  name: Your project
  environment: development
  max_concurrent_runs: 3
  environment_max_concurrent_runs: 2
tracker:
  kind: github
  provider:
    repo: your-org/your-repo
    token: $GITHUB_TOKEN
  required_labels: [tempo]
  active_states: [open]
  terminal_states: [closed]
workspace:
  root: $TEMPO_WORKSPACE_ROOT
hooks:
  after_create: |
    git clone https://github.com/your-org/your-repo.git .
  before_run: |
    git fetch origin
agent:
  max_concurrent_agents: 3
  max_turns: 6
  max_tokens_per_run: 1000000
  max_retries: 2
validation:
  enabled: true
  command_timeout_ms: 1800000
  max_attempts_per_run: 5
codex:
  command: codex app-server
```

Put secrets in `.env`, which is ignored by Git:

```dotenv
GITHUB_TOKEN=github_pat_...
OPENAI_API_KEY=sk-...
```

The GitHub token is used by Tempo on the host side and removed from the Codex child
environment. `github_api` is advertised to Codex so the workflow can read or update GitHub through
the configured credential. Its reach is the token's reach, so use a fine-grained token scoped to
the configured repository.

If you use an existing ChatGPT login instead of `OPENAI_API_KEY`, authenticate the persisted
Codex home with `docker compose run --rm --entrypoint codex tempo login`, then start the service
normally.

## Local validation and pull requests

Tempo does not hard-code a test framework or command. The agent inspects each repository's own
documentation and tooling, then submits its complete build, launch, and test sequence to Tempo's
`project_validation` tool. Tempo executes those commands in the issue workspace, captures their
exit codes and output, and unlocks pull-request creation only after the entire sequence succeeds.
Failed validation is returned to the agent so it can fix the project and try again.

Tempo fingerprints the workspace before and after validation and rejects an attempt if its
commands modify project files. Code changes must happen through normal workspace tools, not through
the validation runner. Successful pull requests automatically receive a `Closes #N` link and Tempo
removes the dispatch label, preventing the issue from being picked up again while it awaits human
review. When validated behavior is already present, the agent can record a reviewed no-change
completion through `tempo_complete`; Tempo comments on the issue and removes the dispatch label.

The bundled Compose setup includes a credential-free validation runner and an isolated Docker
daemon for projects that use containers. The workspace volume is shared with both, so
repository-native commands, Compose files, and bind mounts work at their expected paths. The
runner does not receive the GitHub, OpenAI, or Django credentials or the Codex home. Pull-request
merges are always denied; a human remains responsible for review and merge.

The dashboard reports validation passes as attempts and validated runs as distinct runs with at
least one accepted pass. Neither number means that a pull request was merged.

Repositories still need to describe enough of their setup to run locally. If required services,
credentials, or instructions are unavailable, validation remains blocked and Tempo will not create
a pull request.

## Workflow behavior

`WORKFLOW.md` owns tracker, polling, workspaces, hooks, concurrency, Codex policy, and the prompt.
Prompt variables are `issue` and `attempt`; unknown variables fail the attempt. Relative workspace
paths resolve beside the workflow file, `~` expands, and a path containing only `$VAR` reads that
environment variable.

### Run multiple projects or repositories

Tempo uses one workflow file per project/repository. A single control-plane process can load
several workflow files and runs an independent orchestrator for each one. The dashboard and API
combine their runtime state while preserving project-scoped queues, limits, history, and tracker
configuration.

For example:

```text
projects/
├── api/WORKFLOW.md
└── web/WORKFLOW.md
```

Each workflow must configure:

- A unique `project.organization` and `project.slug` pair. Tempo rejects duplicate project keys at
  startup.
- The repository monitored by that workflow in `tracker.provider.repo`.
- Repository-specific clone and fetch commands in `hooks`.
- A distinct `workspace.root`. GitHub issue identifiers such as `GH-42` are only unique within a
  repository, so sharing a workspace root could make projects use the same issue directory.

An API workflow might begin with:

```yaml
---
project:
  organization: acme
  slug: api
  name: Acme API
  environment: development
  max_concurrent_runs: 3
  environment_max_concurrent_runs: 2
tracker:
  kind: github
  provider:
    repo: acme/api
    token: $GITHUB_TOKEN
  required_labels: [tempo]
  active_states: [open]
  terminal_states: [closed]
workspace:
  root: /data/workspaces/api
hooks:
  after_create: |
    git -c credential.helper='!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
      clone https://github.com/acme/api.git .
  before_run: |
    git -c credential.helper='!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
      fetch origin
---
```

The web workflow would use a different project slug, repository, clone URL, and workspace root such
as `/data/workspaces/web`. A single fine-grained `GITHUB_TOKEN` may cover all configured
repositories; grant it access only to the repositories Tempo needs.

Start both workflows locally by passing every path to `tempo`:

```bash
tempo ./projects/api/WORKFLOW.md ./projects/web/WORKFLOW.md
```

For Docker Compose, mount every workflow into the Tempo container and pass the container paths as
command arguments:

```yaml
services:
  tempo:
    command:
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - /app/workflows/api.md
      - /app/workflows/web.md
    volumes:
      - ./projects/api/WORKFLOW.md:/app/workflows/api.md:ro
      - ./projects/web/WORKFLOW.md:/app/workflows/web.md:ro
      - tempo-workspaces:/data/workspaces
      - tempo-database:/data/database
      - tempo-codex-home:/home/tempo/.codex
```

The validation runner and project runner should keep the shared `tempo-workspaces` volume mounted
at `/data/workspaces`, as in the checked-in Compose configuration. Project-specific workspace roots
can be subdirectories of that shared volume.

Concurrency settings apply independently to each project. For example, two projects with
`max_concurrent_runs: 3` can run up to six jobs in total, subject to each workflow's agent and
environment limits. Editing an already loaded workflow is hot-reloaded, but adding or removing a
workflow path requires updating the startup command and restarting Tempo.

The default security posture is deliberately conservative:

- Codex uses `workspace-write`.
- The generated turn policy writes only under the current issue workspace and disables network.
- Approval requests and interactive input fail the attempt instead of waiting forever.
- Set `codex.approval_policy: never` only in a trusted environment if you want Tempo to accept
  app-server approval callbacks automatically.
- Enable `networkAccess: true` in an explicit `codex.turn_sandbox_policy` only when the task needs
  outbound access.
- Hook scripts are trusted repository policy and run through `bash -lc`; review them like code.
- Repository validation runs project code in the credential-free runner and directs containers to
  the dedicated Compose Docker daemon. Do not replace it with the host Docker socket.

Changes to workflow settings and prompts are reloaded without restart. Invalid changes remain
operator-visible while the last valid configuration stays active.

## API

- `GET /` — live dashboard
- `GET /healthz` — process readiness
- `GET /api/v1/state` — complete runtime snapshot
- `GET /api/v1/events` — live server-sent stream of runtime snapshots
- `GET /api/v1/admin` — redacted effective configuration and operator state
- `GET /api/v1/<issue_identifier>` — running/retry status for one issue
- `POST /api/v1/refresh` — authenticated; wake every project poll loop immediately
- `POST /api/v1/runs/<run_id>/<action>` — authenticated and audited; actions are `pause`,
  `resume`, `cancel`, `retry`, `requeue`, `unblock`, `reprioritize`, and `feedback`
- `GET /api/v1/approvals` — authenticated pending approval inbox
- `POST /api/v1/approvals/<approval_id>/decision` — authenticated approve, edit, or reject

The control center at `/` combines the project portfolio, live agent timelines, durable retry
queue, blocked and paused runs, approval inbox, validation evidence, and direct operator actions.
It uses a reconnecting live stream for agent messages, commands, tool calls, file changes, phases,
tokens, and session details. Private reasoning text is not exposed. Intervention and approval
details appear only to authenticated operators. `/ops/` and `/ops/configuration/` provide compact
runtime and redacted policy views without exposing credentials or hook bodies. Authenticated
Django Admin remains available at `/admin/`.

The HTTP server binds to `127.0.0.1` by default outside Docker. Docker explicitly binds the process
to `0.0.0.0` and publishes port 8000.

## Run locally

Python 3.12+, Git, Bash, and an authenticated Codex CLI are required:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
tempo ./WORKFLOW.md

# Host several projects and trackers in one control-plane process:
tempo ./projects/api/WORKFLOW.md ./projects/web/WORKFLOW.md
```

Useful options:

```bash
tempo --help
tempo --no-http ./WORKFLOW.md
tempo --host 127.0.0.1 --port 9000 ./WORKFLOW.md
```

## Test

Run all quality checks in an isolated container:

```bash
docker build -t tempo-python .
docker run --rm -v "$PWD:/src" -w /src --entrypoint sh tempo-python \
  -lc "pip install -e '.[dev]' && ruff check . && pytest -q"
```

For a real integration check, use a dedicated repository/label, a fine-grained GitHub token, and
an isolated workspace volume. The deterministic suite intentionally makes no network calls.

## Architecture

```text
WORKFLOW.md(s) ──> ControlPlane ──> project Orchestrator(s) ──> leased workers
                         │                    │                    ├─ Workspace + hooks
                         │                    │                    └─ Codex app-server
                         │                    │
                         └──── PostgreSQL durable queue, checkpoints, approvals, and audit
                                      │
                               Django + /api/v1/*
```

After restart, accepted and retry-scheduled runs are restored from PostgreSQL. Expired worker
leases return to the durable queue at their last checkpoint, while live leases prevent a second
control-plane process from claiming the same run. Completed dispositions and safety stops prevent
duplicate dispatch. Terminal workspaces are swept during startup and on terminal transitions.

## Current scope

Each workflow still selects one tracker, but a Tempo process can host several uniquely scoped
project workflows concurrently. It ships GitHub Issues plus a development adapter. Linear, Jira,
Asana, GitLab, and SSH workers remain extension work. Rich tracker mutations remain workflow/tool
policy rather than orchestrator business logic.
