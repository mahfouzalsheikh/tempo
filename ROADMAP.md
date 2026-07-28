# Tempo Product Roadmap

## Product direction

Tempo already has a credible foundation for autonomous engineering: isolated workspaces, safety
budgets, validation-gated publication, durable run history, and strong GitHub protections.

The winning position is not "another generic agent framework." It is:

> The most trustworthy control plane for turning real engineering work into validated, reviewable
> changes.

The current constraints are explicit: scheduling and retries are in memory, one tracker is active
at a time, Codex is the only agent runtime, and operator APIs provide observation but little
intervention.

## Prioritized roadmap

### 1. Durable execution kernel

Replace in-memory claims, retries, timers, and active-run state with PostgreSQL-backed runs, steps,
checkpoints, worker leases, heartbeats, and idempotency keys. Separate the control plane from
horizontally scalable workers.

**Definition of done:** Killing any process loses no accepted work. Runs resume from the last safe
checkpoint without duplicating commits, comments, tool calls, or pull requests.

### 2. Human control and approval inbox

Add authenticated pause, resume, cancel, retry, requeue, unblock, reprioritize, and "send feedback
to agent" actions. Add tool-level approve, edit, and reject gates with durable waiting states.

**Definition of done:** An operator can safely intervene in every active or blocked run without
editing tracker state or restarting Tempo. Every action is audited.

### 3. Multi-project control plane

Make organization, project, repository, environment, workflow, and credential first-class
entities. Support many repositories and trackers concurrently with project quotas and
environment-specific policies.

**Definition of done:** One Tempo deployment can operate a real portfolio without duplicating
services or sharing credentials and limits across projects.

### 4. Typed workflow graphs and agent teams

Evolve the single prompt loop into versioned nodes and edges: sequential steps, conditionals,
fan-out and join, dependency gates, retries, human gates, and specialist agents such as planner,
implementer, reviewer, and tester. Preserve the existing issue-to-pull-request loop as a built-in
template.

**Definition of done:** A workflow can express "plan -> parallel implementation and research ->
review -> validation -> publication," with typed state visible at every node.

### 5. Provider-neutral agent runtime

Introduce `AgentRuntime`, `ModelProvider`, and `ToolProvider` interfaces. Keep Codex app-server as
the premier coding backend while supporting the OpenAI Agents SDK and external runtimes. Add model
routing and fallback by task, cost, and capability.

**Definition of done:** A workflow selects runtimes and models declaratively. Adding a provider
does not require changes to the scheduler or workflow engine.

### 6. MCP-native tool and connector platform

Support MCP over stdio and Streamable HTTP, including tools, resources, prompts, OAuth, capability
discovery, approvals, and progress events. Add a connector SDK for GitHub, GitLab, Jira, Linear,
Slack, Teams, CI systems, and cloud environments. Prefer webhooks while retaining polling as a
fallback.

**Definition of done:** Teams can connect their existing systems without writing Tempo core code,
and each capability has explicit scopes and approval policy.

### 7. Enterprise security and governance

Add project RBAC, SSO and OIDC, immutable audit logs, Vault and cloud-secret integration,
short-lived credentials, per-tool scopes, network egress allowlists, sandbox profiles, retention
controls, and policy as code. Protect all operator APIs and remove unauthenticated state-changing
endpoints.

**Definition of done:** A security review can determine who authorized every external side effect,
which credentials were used, and what data left the workspace.

### 8. End-to-end observability and economics

Emit OpenTelemetry traces for workflow, run, node, turn, tool, and validation-command activity.
Add queue latency, success rate, retry causes, token and dollar cost, validation efficiency, pull
request review and merge outcomes, and SLO alerts. Export through OTLP.

**Definition of done:** Every failure is diagnosable from one correlated trace, and cost and
reliability can be compared by project, workflow version, model, and agent role.

### 9. Evaluation, replay, and release safety

Version and pin workflows, prompts, models, tools, policies, and sandbox images. Turn production
traces into datasets; add graders, regression suites, shadow runs, canaries, run comparison, and
checkpoint replay and forking.

**Definition of done:** No workflow or model revision reaches production unless it beats the
current version on configured quality, safety, cost, and latency thresholds.

### 10. Operator and developer cockpit

Build a visual workflow and run graph, searchable timeline, approval queue, workspace diff and
artifact browser, terminal and log streaming, failure clustering, bulk operations, workflow
linter, local simulator, dry-run mode, CLI and SDK, and reusable templates.

**Definition of done:** A new repository can be onboarded and dry-run successfully from the UI or
CLI, while an operator can understand any run without opening Django Admin or inspecting
containers.

## Recommended sequence

### Foundation

1. Durable execution kernel
2. Human control and approval inbox
3. Multi-project control plane

This makes Tempo trustworthy for actual teams and prevents later graph features from being built
on ephemeral state.

### Orchestration platform

4. Typed workflow graphs and agent teams
5. Provider-neutral agent runtime
6. MCP-native tool and connector platform
7. Enterprise security and governance

This expands Tempo from one Codex and GitHub loop into a secure, interoperable engineering-agent
platform.

### Operational excellence

8. End-to-end observability and economics
9. Evaluation, replay, and release safety
10. Operator and developer cockpit

This creates the feedback loop needed to improve quality without relying on anecdotes.

If only three items are funded next, build durable execution, human control, and multi-project
support. Those transform Tempo from a strong autonomous-worker prototype into infrastructure teams
can responsibly depend on.

## Industry references

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Temporal Workflow Execution](https://docs.temporal.io/workflow-execution)
- [OpenAI Agents SDK architecture guidance](https://developers.openai.com/cookbook/examples/agents_sdk/migrate-from-claude-agent-sdk/readme#what-you-migrate)
- [Model Context Protocol architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [OpenAI trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
