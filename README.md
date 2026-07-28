# Tempo

A Dockerized Python implementation of OpenAI's
[autonomous coding service specification](https://github.com/openai/symphony/blob/main/SPEC.md).
It continuously polls an issue tracker, creates an isolated workspace for each issue, and runs
Codex app-server sessions until the issue leaves an active state.

This port uses Django for the operator dashboard, JSON API, authenticated admin, and persistent
runtime history. The scheduler and retry queue remain in one authoritative in-memory orchestrator,
so Django's database records activity without becoming a competing job queue.

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
- Structured JSON logs, Django dashboard, health check, and REST status endpoints
- A non-root Docker image containing Python, Git, SSH, Node, and the Codex CLI
- Deterministic tests with fake tracker and app-server implementations

## Quick start

The checked-in workflow uses an empty in-memory tracker, so the service starts safely without
dispatching work:

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

`WORKFLOW.md` owns tracker, polling, workspaces, hooks, concurrency, Codex policy, and the prompt.
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

Changes to workflow settings and prompts are reloaded without restart. Invalid changes remain
operator-visible while the last valid configuration stays active.

## API

- `GET /` — live dashboard
- `GET /healthz` — process readiness
- `GET /api/v1/state` — complete runtime snapshot
- `GET /api/v1/events` — live server-sent stream of runtime snapshots
- `GET /api/v1/admin` — redacted effective configuration and operator state
- `GET /api/v1/<issue_identifier>` — running/retry status for one issue
- `POST /api/v1/refresh` — wake the poll loop immediately

The dashboard at `/` uses a reconnecting live stream to show agent messages, commands and output,
tool calls, file changes, validation progress, errors, phases, tokens, and session details as they
happen. Private reasoning text is not exposed. Operator pages at `/ops/` and
`/ops/configuration/` show runtime and redacted policy details without exposing credentials or hook
bodies. Authenticated Django Admin is available at `/admin/`.

The HTTP server binds to `127.0.0.1` by default outside Docker. Docker explicitly binds the process
to `0.0.0.0` and publishes port 8000.

## Run locally

Python 3.12+, Git, Bash, and an authenticated Codex CLI are required:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
tempo ./WORKFLOW.md
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
WORKFLOW.md ──> WorkflowStore ──> typed config + strict prompt
                                      │
GitHub / memory tracker ──> Orchestrator ──> per-issue worker
                               │                  ├─ WorkspaceManager + hooks
                               │                  └─ Codex app-server JSONL
                               │
                               ├─ Django dashboard + /api/v1/*
                               └─ SQLite history + Django Admin
```

After restart, candidates are recovered from the tracker and workspaces from disk. Completed
dispositions and safety stops are restored from SQLite so those issues are not dispatched again.
Exact retry timers are not restored, but issue, run, session, validation, command, token, error,
and pull-request history remains available through Django Admin. Terminal workspaces are swept
during startup and when terminal transitions are observed.

## Current scope

This implementation conforms around one selected tracker as required by the core specification.
It ships GitHub Issues plus a development adapter. Linear, Jira, Asana, GitLab, SSH workers, and a
durable retry queue are extension work, not required for the core scheduler. Rich tracker mutations
remain workflow/tool policy rather than orchestrator business logic.
