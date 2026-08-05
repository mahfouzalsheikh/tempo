# Tempo

Tempo is a Python and Django control plane for autonomous coding work. It polls one or more issue
trackers, leases eligible issues, creates an isolated workspace for each issue, and drives Codex
app-server through implementation, project-native validation, pull-request review, and merge or
human handoff.

The service currently supports GitHub Issues and a deterministic in-memory adapter. GitHub is the
only production tracker implementation, and Codex app-server is the only agent runtime.

For the complete internal design, data model, lifecycle, recovery behavior, and security
boundaries, read [System architecture](docs/ARCHITECTURE.md). Future work is tracked in
[ROADMAP.md](ROADMAP.md).

## What works today

- One independently configured orchestrator per `WORKFLOW.md`, with several workflows hosted by
  one process
- GitHub issue polling, label-based routing, priority ordering, concurrency limits, and terminal
  issue cleanup
- Durable runs, claims, leases, heartbeats, retries, checkpoints, completion dispositions, and
  operator actions in PostgreSQL or SQLite
- Root-contained issue workspaces with create, pre-run, post-run, and pre-remove hooks
- Separate Codex implementation and review threads, including durable thread resumption
- Repository-native validation with command output capture and workspace fingerprinting
- Validation-gated GitHub writes, no-change completion, independent review, optional automatic
  merge, and explicit human handoff
- JWT operator login, audited run controls, approval decisions, a live Django dashboard, JSON
  APIs, and server-sent state updates
- A Docker Compose deployment with PostgreSQL, a credential-free validation service, and a
  dedicated Docker daemon for validation workloads

## Quick start with Docker Compose

Requirements:

- Docker with Compose
- A GitHub token scoped to the repository configured in `WORKFLOW.md`
- Either `OPENAI_API_KEY` or an authenticated Codex login

Create the local environment file and set the required credentials:

```bash
cp .env.example .env
# Edit .env and set GITHUB_TOKEN plus your Codex authentication.
```

The checked-in `WORKFLOW.md` points at
`mahfouzalsheikh/drawing-algorithms`. Change its project, repository, hook URLs, labels, and
reviewers before using Tempo for another repository.

Start the stack:

```bash
docker compose up --build
```

Open <http://localhost:8030>. The port can be changed with `TEMPO_PORT` in `.env`.

The first operator can be created interactively:

```bash
docker compose exec tempo python manage.py createsuperuser
```

Alternatively, set `TEMPO_ADMIN_USERNAME`, `TEMPO_ADMIN_PASSWORD`, and optionally
`TEMPO_ADMIN_EMAIL` before the first startup. Sign in at `/login/`.

When `OPENAI_API_KEY` is empty, Compose seeds a container-owned Codex state volume from the host's
existing `~/.codex/auth.json` and `config.toml`. Confirm that the host is authenticated before
startup:

```bash
codex login status
docker compose up
```

Compose persists PostgreSQL data, workspaces, SQLite fallback data, and mutable Codex state in
named volumes. `docker compose down` preserves all of them; `docker compose down -v`
intentionally deletes the named volumes but does not delete host Codex state.

The checked-in workflow uses Codex App Server's `externalSandbox` turn policy because Docker is
the execution boundary and nested `bwrap` namespaces are commonly blocked by container security
profiles. Only run repositories you trust in this deployment: commands can access the Tempo
container, although tracker credentials are removed from the agent command environment and GitHub
writes remain mediated by Tempo's provider tool.

The Tempo service relaxes Docker's outer seccomp profile so Codex can create its inner Linux
`workspace-write` sandbox with `bwrap`. Codex commands remain restricted to the issue workspace;
the validation and Docker-in-Docker services keep their separate isolation boundaries.

After source changes, rebuild and health-check all services with:

```bash
./scripts/restart-tempo.sh
```

## Local development with Pipenv

Requirements:

- Python 3.12
- Pipenv
- Git and Bash
- The Codex CLI, authenticated with an API key or persisted login

Install the exact locked application and development dependencies:

```bash
cp .env.example .env
pipenv sync --dev
```

Set `GITHUB_TOKEN` in `.env`, adjust `WORKFLOW.md`, then start Tempo:

```bash
pipenv run start
```

Pipenv loads `.env` automatically. The example sets `TEMPO_WORKSPACE_ROOT=./var/workspaces`; local
runs use SQLite at `./var/tempo.sqlite3` unless `DATABASE_URL` is set.

Useful commands:

```bash
pipenv run tempo --help
pipenv run start-no-http
pipenv run tempo --host 127.0.0.1 --port 9000 ./WORKFLOW.md
pipenv run lint
pipenv run test
pipenv run check
```

To host several projects in one process, give each workflow a unique
`project.organization`/`project.slug`, repository, and workspace root:

```bash
pipenv run tempo ./projects/api/WORKFLOW.md ./projects/web/WORKFLOW.md
```

Add or update dependencies through Pipenv and commit both dependency files:

```bash
pipenv install package-name
pipenv install --dev package-name
pipenv lock
```

`Pipfile` is the dependency source of truth and `Pipfile.lock` supplies reproducible versions and
hashes. `pyproject.toml` contains package metadata, the `tempo` console entry point, and tool
configuration; it intentionally does not duplicate dependency declarations.

## Workflow configuration

Every workflow is a Markdown file with YAML front matter followed by a Jinja prompt:

```markdown
---
project:
  organization: acme
  slug: api
  name: Acme API
  environment: development
tracker:
  kind: github
  provider:
    repo: acme/api
    token: $GITHUB_TOKEN
  required_labels: [tempo]
  active_states: [open]
  terminal_states: [closed]
workspace:
  root: $TEMPO_WORKSPACE_ROOT
hooks:
  after_create: |
    git clone https://github.com/acme/api.git .
agent:
  max_concurrent_agents: 3
validation:
  enabled: true
review:
  enabled: true
  auto_merge: false
codex:
  command: codex app-server
---

Work on {{ issue.identifier }}: {{ issue.title }}.
```

Only `issue` and `attempt` are valid prompt variables. Configuration is validated with Pydantic.
Tempo watches each workflow's modification time and applies a valid change on the next tick; an
invalid edit is reported while the last valid definition remains active.

The checked-in [WORKFLOW.md](WORKFLOW.md) provides a complete working configuration example. The
behavior and defaults of every section are described in
[System architecture](docs/ARCHITECTURE.md#workflow-and-configuration).

## Validation and publication

When validation is enabled, Codex must call Tempo's `project_validation` tool with the repository's
own build, launch, and test commands. Tempo runs the sequence, stops at the first failed command,
always runs an optional cleanup command, and records the result.

A successful run is accepted only if validation did not change tracked or untracked project files.
The resulting workspace fingerprint authorizes GitHub writes. Any later workspace change
invalidates that authorization and requires another validation run.

After the implementation agent creates a pull request:

1. Tempo starts a separate review thread when review is enabled.
2. The reviewer inspects and may fix the branch, then validates the final workspace.
3. The reviewer records `approve` or `human_review`.
4. Tempo—not either agent—applies the configured merge policy.

If review is disabled, automatic review and merge do not occur; Tempo creates a human-review
handoff. If no code change is required, a validated implementation agent can finish with
`tempo_complete`.

## HTTP surfaces

The main pages are:

- `/` — live control center
- `/login/` — operator sign-in
- `/ops/` — runtime and intervention view
- `/ops/configuration/` — redacted effective configuration
- `/admin/` — Django Admin inspection
- `/healthz` — readiness

Core API routes:

| Method | Route | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/v1/state` | No | Complete live snapshot |
| `GET` | `/api/v1/events` | No | Server-sent snapshots and keepalives |
| `GET` | `/api/v1/admin` | No | Redacted effective configuration |
| `GET` | `/api/v1/<identifier>` | No | Active or retrying issue status |
| `POST` | `/api/v1/refresh` | Yes | Wake all poll loops |
| `GET` | `/api/v1/control` | Yes | Paused, approval-blocked, and safety-stopped runs |
| `POST` | `/api/v1/runs/<id>/<action>` | Yes | Apply an audited run action |
| `GET` | `/api/v1/approvals` | Yes | List pending approvals |
| `POST` | `/api/v1/approvals/<id>/decision` | Yes | Approve or reject, optionally editing arguments |
| `POST` | `/api/v1/auth/login` | No | Exchange Django credentials for a JWT and cookie |
| `POST` | `/api/v1/auth/logout` | No | Clear the JWT cookie |
| `GET` | `/api/v1/auth/me` | Yes | Return the current operator |
| `GET` | `/api/v1/platform` | Yes | Inspect workflow graphs, agent teams, and providers |
| `POST` | `/api/v1/platform/<org>/<project>` | Yes | Validate and revise live workflow configuration |

Run actions are `pause`, `resume`, `cancel`, `retry`, `requeue`, `unblock`, `reprioritize`, and
`feedback`. API clients can use `Authorization: Bearer <token>`. Browser mutations use an
HttpOnly, SameSite=Lax JWT cookie and retain CSRF protection.

## Production notes

The shipped settings are development-oriented. Before exposing Tempo outside a trusted network:

- Set a long, random `DJANGO_SECRET_KEY`.
- Serve it behind HTTPS and set `TEMPO_JWT_COOKIE_SECURE=true`.
- Restrict network access to the unauthenticated read-only dashboard and state/configuration APIs
  if their operational metadata is sensitive.
- Replace the default PostgreSQL password and scope GitHub credentials to only required
  repositories and permissions.
- Protect database and Django Admin access. Workflow versions currently persist the resolved
  effective configuration, including provider credential values.
- Review the configured Codex approval policy. The checked-in workflow uses `never`, which
  auto-accepts Codex command/file approval requests for the session.
- Treat the Compose Docker-in-Docker validation service as privileged infrastructure.

## Test guarantees and current limits

The deterministic suite uses fake tracker and app-server implementations and makes no external
network calls:

```bash
pipenv run check
```

Current limitations are deliberate:

- GitHub Issues and the in-memory test adapter are the only trackers.
- Scheduling uses polling, not webhooks.
- Workers run inside the control-plane process; leases support recovery but not an independently
  scalable worker service.
- Workflow graphs support agent, human-gate, and join nodes with conditional edges and bounded
  parallel execution; richer event-trigger and reusable subworkflow semantics remain future work.
- SQLite is suitable for local development and tests; Compose uses PostgreSQL for durable
  multi-process-safe claims.
- The application has no project-scoped RBAC or SSO.

## Database-backed workflow configuration

On first startup, each project's `WORKFLOW.md` seeds an authoritative `Workflow configuration`
record in PostgreSQL. Authenticated operators can edit its workflow graph, specialist agents,
runtime providers, model routes, and tool providers in Django Admin or `/ops/configuration/`.
Tempo validates references and graph acyclicity before activation, versions the effective workflow,
and reloads saved database changes without a container restart. File-backed tracker, workspace,
polling, review, and prompt policy continues to reload from `WORKFLOW.md`.
