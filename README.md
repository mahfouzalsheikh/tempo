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
- Typed DAG workflows with sequential nodes, conditional edges, fan-out, bounded parallelism,
  joins, human gates, dependency gates, per-node retries, and durable node checkpoints
- Specialist agent profiles with role prompts, isolated runtime sessions, shared-workspace handoffs,
  per-node telemetry, and live graph state
- Registry-backed `AgentRuntime`, `ModelProvider`, and `ToolProvider` interfaces with native Codex
  and external JSONL/OpenAI Agents SDK bridge runtimes
- Declarative role, capability, and cost-aware model routing with ordered model fallbacks and named
  tool bundles
- Structured JSON logs, Django dashboard, health check, and REST status endpoints
- A non-root Docker image containing Python, Git, SSH, Node, and the Codex CLI
- Deterministic tests with fake tracker and app-server implementations

## Quick start

The checked-in workflow targets the repository configured in `WORKFLOW.md`. Tempo dispatches only
open issues carrying its required label; review that file and `.env` before starting the service:

```bash
docker compose up --build
```

Open <http://localhost:8000>. Verify health with:

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/api/v1/state
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

If you use an existing ChatGPT login instead of `OPENAI_API_KEY`, copy or mount its Codex home into
the `tempo-codex-home` volume before starting the service.

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

`WORKFLOW.md` owns tracker, polling, workspaces, hooks, concurrency, Codex policy, and the prompt. On
first startup it also seeds the database record for the workflow graph, specialist agents,
runtimes, models, and tools. That `Workflow configuration` record is authoritative thereafter and
can be edited in Django Admin without rebuilding or restarting Tempo.
Prompt variables are `issue` and `attempt`; unknown variables fail the attempt. Relative workspace
paths resolve beside the workflow file, `~` expands, and a path containing only `$VAR` reads that
environment variable.

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

Changes to file-backed service settings and prompts, plus database-backed workflow configuration,
are reloaded without restart. Invalid changes remain operator-visible while the last valid
configuration stays active.

## Workflow graphs and agent teams

Existing workflow files remain compatible: Tempo synthesizes one `implementation` node using the
Codex runtime, default model route, all native tools, and the original issue-to-pull-request
completion policy. An explicit specialist workflow can be configured in YAML:

```yaml
runtime_providers:
  codex:
    kind: codex
  research-bridge:
    kind: openai-agents
    command: python -m your_agents_bridge

model_providers:
  engineering:
    kind: openai
    model: primary-model
    fallbacks: [fallback-model]
    routes:
      - model: review-model
        fallbacks: [primary-model]
        roles: [reviewer]
        capabilities: [code-review]
        max_cost_per_million_tokens: 5

tool_providers:
  read-only:
    kind: tempo
    allow_all: false
    tools: []
  engineering:
    kind: tempo
    allow_all: true

agents:
  planner:
    role: planner
    runtime: research-bridge
    model: engineering
    tool_providers: [read-only]
    completion: turn
    prompt: Write the implementation plan into the shared workspace.
  implementer:
    role: implementer
    runtime: codex
    model: engineering
    tool_providers: [engineering]
    completion: turn
  reviewer:
    role: reviewer
    runtime: codex
    model: engineering
    tool_providers: [engineering]
    capabilities: [code-review]
    completion: validation
  publisher:
    role: publisher
    runtime: codex
    model: engineering
    tool_providers: [engineering]
    completion: publication

workflow:
  name: plan-and-deliver
  max_parallel_nodes: 2
  require_publication: true
  nodes:
    - {id: plan, type: agent, agent: planner}
    - {id: api, type: agent, agent: implementer, max_retries: 1}
    - {id: web, type: agent, agent: implementer, max_retries: 1}
    - {id: review, type: agent, agent: reviewer, settings: {join: all}}
    - {id: review-gate, type: human_gate, approval_message: Review implementation}
    - {id: publish, type: agent, agent: publisher}
  edges:
    - {from: plan, to: api, condition: succeeded}
    - {from: plan, to: web, condition: succeeded}
    - {from: api, to: review, condition: succeeded}
    - {from: web, to: review, condition: succeeded}
    - {from: review, to: review-gate, condition: succeeded}
    - {from: review-gate, to: publish, condition: succeeded}
```

Edges accept `succeeded`, `failed`, `skipped`, `always`, `completed`, `issue.label:<label>`, and
`not issue.label:<label>`. Multiple outgoing edges form a fan-out. Nodes with multiple incoming
edges use an all-join by default; set `settings.join: any` for an any-join. Ready nodes run in
batches bounded by `max_parallel_nodes`. All agents for an issue share its workspace, so parallel
roles should either be read-only or own non-overlapping files.

Agent completion modes are `turn`, `validation`, and `publication`. A model route matches an agent
when its role, required capabilities, and optional cost ceiling match. Node retries automatically
advance through the selected route's model and fallback list before failing the node.

The `external` and `openai-agents` runtime kinds launch the configured command in the issue
workspace and speak JSONL on standard input/output. The bridge receives `session/start`,
`turn/run`, and `session/stop` requests, may stream event objects between request and response, and
must reply with `{"id": <request-id>, "result": {...}}`. This lets an OpenAI Agents SDK or another
runtime integrate without changing Tempo's scheduler or graph executor. Secrets owned by Tempo are
removed from the bridge process environment.

Authenticated operators can inspect and edit these five platform sections from
`/ops/configuration/` or the editable `Workflow configuration` model in Django Admin. Tempo
validates references and graph acyclicity, stores a revisioned PostgreSQL definition, schedules a
live reload, and records API changes as accepted or rejected. The checked-in `WORKFLOW.md` supplies
the initial database seed, so read-only container mounts remain compatible and live edits survive
rebuilds. Generated runtime/provider/profile/node catalogs and every run's node execution history
are also available in Django Admin as read-only runtime records.

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
- `GET /api/v1/platform` — authenticated effective workflow graphs, teams, and providers
- `POST /api/v1/platform/<organization>/<project>` — authenticated validation and durable update of
  runtime, model, tool, agent, and workflow sections

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
                                      │
                    typed DAG + AgentRuntime/ModelProvider/ToolProvider registries
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
