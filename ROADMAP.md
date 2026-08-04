# Tempo Product Roadmap

## Product direction

Tempo is a trustworthy control plane for turning real engineering work into validated, reviewable
changes. The platform now has a durable PostgreSQL execution kernel, concurrent multi-project
hosting, authenticated operator controls, approval gates, isolated validation, independent
pull-request review, policy-controlled merging, and a responsive control center.

The remaining constraints are explicit: workflows have one built-in implement-review sequence
rather than a general graph, GitHub Issues and the in-memory adapter are the only tracker
implementations, workers are managed by the control-plane process, and enterprise identity,
generic MCP connectivity, OpenTelemetry, evaluation, and replay are not yet implemented.

Status labels used below:

- **Implemented** — available in the current platform and covered by tests.
- **Foundation implemented** — the core capability exists; scale or enterprise hardening remains.
- **In progress** — useful parts are available, but the definition of done is not yet met.
- **Planned** — not yet implemented.

## Current platform

The following capabilities are implemented:

- PostgreSQL-backed accepted work, retry schedules, checkpoints, leases, heartbeats, idempotency
  keys, safety stops, and durable completion dispositions.
- Authenticated and audited pause, resume, cancel, retry, requeue, unblock, reprioritize, feedback,
  refresh, and approval-decision APIs.
- A first-class operator login using expiring signed JWTs, HttpOnly browser cookies, Bearer-token
  API authentication, and CSRF protection for cookie-authenticated mutations.
- Durable approval requests with approve, edit, and reject decisions.
- First-class organizations, projects, repositories, environments, workflow versions, credential
  references, and per-project/environment concurrency limits.
- Concurrent hosting of multiple workflow files and project orchestrators in one control plane.
- GitHub Issues and deterministic in-memory tracker adapters.
- Codex app-server sessions with durable implementation/review thread resumption across unblock,
  retry, lease recovery, and restart; fresh per-attempt safety budgets; explicit checkpoint
  fallback; timeouts; token/rate-limit telemetry; host-side GitHub tooling; and approval/input
  handling.
- A built-in two-stage workflow with separate implementation and review threads,
  validation-gated review decisions, policy-controlled automatic merges, and explicit GitHub human
  handoffs when risk or repository rules require a person.
- Isolated issue workspaces, lifecycle hooks, credential filtering, a credential-free validation
  runner, a dedicated Docker daemon, and validation-gated pull-request publication.
- Structured logs, health and state APIs, server-sent runtime events, live activity timelines,
  project health, retry and approval views, and responsive operator/runtime/configuration screens.
- Automatic migrations, optional initial superuser creation, Docker Compose startup, persisted
  PostgreSQL/workspace/Codex volumes, and deterministic lint/test coverage.

## Prioritized roadmap

### 1. Durable execution kernel — Foundation implemented

Accepted work, claims, retries, checkpoints, worker leases, heartbeats, idempotency keys, and
completion state are authoritative in PostgreSQL. Restart reconciliation and lease expiry prevent
accepted work from being silently lost. Operator unblocks, scheduled retries, lease recovery, and
service restarts resume the durable Codex thread without replaying the original issue prompt; an
unavailable thread falls back to explicit workspace/tracker/checkpoint reconstruction.

Remaining work:

- Separate workers from the control-plane process.
- Add independently scalable worker pools and queue partitions.
- Expand idempotency coverage for every external provider side effect.
- Add chaos and failover testing across multiple worker processes.

**Definition of done:** Killing any process loses no accepted work. Runs resume from the last safe
checkpoint without duplicating commits, comments, tool calls, or pull requests, and workers scale
independently from the web/control-plane process.

### 2. Human control and approval inbox — Implemented

Tempo supports authenticated pause, resume, cancel, retry, requeue, unblock, reprioritize,
feedback, and refresh actions. Tool approvals are durable and support approve, edit, and reject
decisions. Operator actions are audited.

Remaining hardening belongs to project RBAC, immutable audit storage, and enterprise identity in
the security milestone.

**Definition of done:** An operator can safely intervene in every active or blocked run without
editing tracker state or restarting Tempo. Every action is audited.

### 3. Multi-project control plane — Foundation implemented

Organizations, projects, repositories, environments, workflow versions, credential references,
quotas, and concurrent workflow hosting are first-class. One process can operate multiple project
orchestrators without sharing project concurrency limits.

Remaining work:

- Add UI-driven project onboarding and lifecycle management.
- Add external secret backends and credential rotation.
- Add project-scoped roles and stronger tenant isolation.
- Prove horizontal scaling across multiple control-plane instances.

**Definition of done:** One Tempo deployment can operate a real portfolio without duplicating
services or sharing credentials, limits, or authorization boundaries across projects.

### 4. Typed workflow graphs and agent teams — Implemented

The built-in issue workflow now runs separate implementer and reviewer Codex threads. The reviewer
has a typed `approve`/`human_review` disposition, must revalidate before approval, and hands control
to a merge-policy step. GitHub merge restrictions, required checks, permissions, and explicit
reviewer uncertainty become a visible human-review outcome with reviewer assignment and issue/PR
notifications.

Implemented capabilities now include versioned nodes and edges, conditionals, bounded parallel
fan-out, all/any joins, reusable human gates, configurable specialist profiles, and durable
per-node execution state. Workflow, agent, runtime, model, and tool policy is editable as validated
JSON through Django Admin or the operator configuration API.

**Definition of done:** A workflow can express "plan -> parallel implementation and research ->
review -> validation -> publication," with typed state visible at every node.

### 5. Provider-neutral agent runtime — Implemented

`AgentRuntime`, `ModelProvider`, and `ToolProvider` interfaces keep Codex app-server as the premier
coding backend while supporting OpenAI Agents SDK and external JSONL runtime bridges. Declarative
model routing and fallback can select by role, capability, and cost ceiling.

**Definition of done:** A workflow selects runtimes and models declaratively. Adding a provider
does not require changes to the scheduler or workflow engine.

### 6. MCP-native tool and connector platform — Planned

Support MCP over stdio and Streamable HTTP, including tools, resources, prompts, OAuth, capability
discovery, approvals, and progress events. Add a connector SDK for GitHub, GitLab, Jira, Linear,
Slack, Teams, CI systems, and cloud environments. Prefer webhooks while retaining polling as a
fallback.

The current host-side GitHub tool demonstrates scoped provider tooling but is not yet a generic MCP
connector layer.

**Definition of done:** Teams can connect their existing systems without writing Tempo core code,
and each capability has explicit scopes and approval policy.

### 7. Enterprise security and governance — In progress

Implemented security foundations include JWT-authenticated state-changing APIs, audited operator
actions, workspace-write sandboxing, filtered child environments, credential-free validation,
isolated Docker execution, explicit approval policy, and policy-controlled pull-request merging.

Remaining work:

- Project RBAC, SSO, OIDC, and delegated administration.
- Immutable audit export and retention controls.
- Vault and cloud-secret integration with short-lived credentials.
- Per-tool scopes, network egress allowlists, and policy as code.
- Production HTTPS, secure-cookie, proxy, and deployment hardening.

**Definition of done:** A security review can determine who authorized every external side effect,
which credentials were used, and what data left the workspace.

### 8. End-to-end observability and economics — In progress

Tempo currently exposes structured logs, live event streams, run phases, activity timelines,
validation commands, retry state, token totals, runtime duration, safety stops, and rate-limit
telemetry.

Remaining work:

- OpenTelemetry traces across workflows, runs, turns, tools, and validation.
- Queue-latency, success-rate, retry-cause, and SLO dashboards.
- Dollar-cost accounting and project/workflow/model comparisons.
- Historical pull-request review and merge outcome analytics across external activity.
- OTLP export and alerting.

**Definition of done:** Every failure is diagnosable from one correlated trace, and cost and
reliability can be compared by project, workflow version, model, and agent role.

### 9. Evaluation, replay, and release safety — In progress

Workflow versions and durable checkpoints provide the persistence base for evaluation and replay.
Run limits, validation evidence, and last-known-good workflow reloads already prevent several
unsafe release paths.

Remaining work:

- Pin models, tools, policies, and sandbox images with each workflow version.
- Turn production traces into evaluation datasets.
- Add graders, regression suites, shadow runs, canaries, and run comparison.
- Add checkpoint replay, forking, and deterministic side-effect simulation.

**Definition of done:** No workflow or model revision reaches production unless it beats the
current version on configured quality, safety, cost, and latency thresholds.

### 10. Operator and developer cockpit — In progress

The current responsive control center includes project health, equal-width metrics, live run
timelines, validation evidence, retry state, approval and attention queues, operator actions,
runtime policy, and redacted configuration views. Django Admin remains available for data
inspection but is not part of the product navigation.

Remaining work:

- Visual workflow and run graphs.
- Search, filtering, failure clustering, and bulk operations.
- Workspace diff, artifact, terminal, and log-streaming views.
- UI project onboarding, workflow editor/linter, simulator, and dry-run mode.
- Supported CLI/SDK automation and reusable workflow templates.

**Definition of done:** A new repository can be onboarded and dry-run successfully from the UI or
CLI, while an operator can understand any run without opening Django Admin or inspecting
containers.

## Recommended sequence

### Completed foundation

1. Durable execution kernel foundation
2. Human control and approval inbox
3. Multi-project control-plane foundation
4. Responsive operator control center
5. Isolated validation and publication gate

### Next: orchestration platform

1. Generalized typed workflow graphs and expanded agent teams
2. Provider-neutral agent runtime
3. MCP-native tool and connector platform

These capabilities expand Tempo beyond one Codex and GitHub loop while building on the durable
state and operator controls already in place.

### Then: production governance

1. Enterprise security and governance
2. Independent worker scaling and failover
3. End-to-end observability and economics

This sequence establishes strong authorization, deployment, and diagnostic boundaries before
broader adoption.

### Then: continuous improvement

1. Evaluation, replay, and release safety
2. Complete operator and developer cockpit
3. Workflow templates, onboarding, CLI, and SDK

If only three initiatives are funded next, prioritize typed workflow graphs, the provider-neutral
runtime, and MCP-native connectors. The original durable-execution, human-control, and
multi-project foundation priorities are now substantially implemented.

## Industry references

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Temporal Workflow Execution](https://docs.temporal.io/workflow-execution)
- [OpenAI Agents SDK architecture guidance](https://developers.openai.com/cookbook/examples/agents_sdk/migrate-from-claude-agent-sdk/readme#what-you-migrate)
- [Model Context Protocol architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [OpenAI trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
