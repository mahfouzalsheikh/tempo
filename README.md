# Tempo

Current next steps are tracked in the [software factory plan](docs/SOFTWARE_FACTORY_PLAN.md).
The [agent account plan](docs/AGENT_ACCOUNTS.md) covers multiple Codex/Claude connections and
team assignments. [Checked task contracts](docs/TASK_CONTRACTS.md) add opt-in capability and
deliverable checks to reviewed product plans.

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
- A Docker Compose deployment with PostgreSQL, an authenticated validation service, and a
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

If using API-key authentication, uncomment the `codex.environment.OPENAI_API_KEY` grant in
`WORKFLOW.md`. Runtime API keys are no longer inherited automatically. Hooks have their own
explicit credential grants. See [credential references and upgrade notes](docs/CREDENTIALS.md).

Start the stack:

```bash
./scripts/restart-tempo.sh
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
./scripts/restart-tempo.sh
```

Compose persists PostgreSQL data, workspaces, SQLite fallback data, and mutable Codex state in
named volumes. `docker compose down` preserves all of them; `docker compose down -v`
intentionally deletes the named volumes but does not delete host Codex state.

The checked-in workflow uses Codex App Server's `externalSandbox` turn policy. Compose now
runs coding agents, reviewers, external runtimes, and hooks in disposable containers with a task
workspace mount, resource limits, and a restricted network. Agent nodes receive private persistent
homes for login and session history. Hooks receive only their explicit grants. See
[runtime isolation](docs/RUNTIME_ISOLATION.md) for upgrade behavior and remaining work.

Validation uses a stricter container with no network access. The
[execution network policy](docs/EXECUTION_NETWORK.md) blocks other job containers from reaching
the daemon, sibling containers, and private infrastructure. Graph nodes can use independent
repositories with `workspace: isolated`; Tempo integrates their committed changes before downstream
work. Nodes using the integration checkout run exclusively. See
[contributions and integration](docs/CONTRIBUTION_INTEGRATION.md) for configuration and recovery.

New runs retain a [verified execution snapshot](docs/RUN_SNAPSHOTS.md). Workflow edits apply to
newly queued work; existing runs and retries keep their original prompts, policies, and defaults.

Saved builds offer release readiness and [reviewed local staging configuration](docs/LOCAL_STAGING.md).
The staging adapter supports immutable static bundles and [recorded rollback rehearsals](docs/ROLLBACK_REHEARSAL.md).
Once all gates pass, [publish the retained build to local staging](docs/STAGING_PUBLICATION.md)
and follow its release history, health evidence, and rollback controls.

After source changes, rebuild and health-check all services with:

```bash
./scripts/restart-tempo.sh
```

The script provisions runner authentication, loads the immutable validation image into the
execution daemon, backs up PostgreSQL, preserves data volumes, and checks the deployed commit
and sandbox execution, including runtime initialization and login recognition. See [access and deployment notes](docs/ACCESS_AND_DEPLOYMENT.md).

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

The default tests use SQLite. To exercise PostgreSQL row locking with independent worker
processes, run the suite against a dedicated PostgreSQL test service:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/tempo_test pipenv run test
```

The database role must be able to create and drop pytest's test database. The two process-race
tests in `tests/test_leases.py` are skipped on SQLite.

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

Enabled validation defaults to a required policy. Configure `validation.required_checks` in the
host workflow: `project_validation` runs those checks before any agent-supplied extras. A missing
policy stops publication runs before agent launch. Failed checks or cleanup prevent a pass.

See [required validation policy](docs/VALIDATION_POLICY.md) for configuration, migration
`0010_validation_policy_evidence`, and the explicit `policy: discovered` compatibility mode.
Retained validation evidence and review approvals must match the current policy before reuse.

A successful run is accepted only if validation did not change tracked or untracked project files.
The resulting fingerprint authorizes `github_publish`, which sends the clean local commit to a
fixed run branch and opens or recovers its PR. Any later content or executable-mode change requires
revalidation. `github_api` is read-only and repository scoped; `github_comment` posts source-issue
updates. Generic API writes stay denied after validation. No-change completion must match the
recorded task base.

Existing saved tool allowlists must explicitly include `github_publish`; update custom prompts
that request shell pushes or raw GitHub mutations. See [publication and upgrade notes](docs/PUBLICATION.md)
for branch ownership, interruption recovery, supported Git inputs, and retained-run compatibility.

After the implementation agent creates a pull request:

1. Tempo starts a separate review thread when review is enabled.
2. The reviewer inspects and may fix the branch, commits all changes, and validates the clean
   commit. It publishes that commit and repeats validation if the final PR commit differs.
3. The reviewer records `approve` or `human_review`.
4. Tempo—not either agent—applies the configured merge policy.

Approvals record the exact reviewed commit. Tempo checks that it matches the remote PR head and
passes that SHA to GitHub's review and merge operations. A changed or unverifiable head requires
fresh review. Saved approvals without a commit identity are handed off for human review after
restart. A new review-validation attempt revokes any saved review decision.

If review is disabled, automatic review and merge do not occur; Tempo creates a human-review
handoff. If no code change is required, a validated implementation agent can finish with
`tempo_complete`.

## Operator interface

The overview highlights decisions and stopped work before active runs. Search active work by
issue/title or filter it by project; expand a run for its steps and check evidence. Project cards
show validation setup, while usage totals and diagnostics live under System details. See the
[product experience plan](docs/PRODUCT_EXPERIENCE.md) for the ongoing UI redesign.

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
- Operational pages and APIs require sign-in. Keep the deployment on a trusted network;
  per-project authorization and isolated task execution remain planned work.
- Replace the default PostgreSQL password and scope GitHub credentials to only required
  repositories and permissions.
- Protect database and Django Admin access. New workflow snapshots retain credential references;
  migration 0011 redacts historical tracker credentials. Protect backups and rotate exposed keys.
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
- Worker writes check lease ownership transactionally. GitHub writes check it before sending;
  requests already in flight still require reconciliation. Process-group cleanup does not provide
  task sandbox isolation.
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
