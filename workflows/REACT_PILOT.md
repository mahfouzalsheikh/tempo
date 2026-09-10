---
project:
  organization: mahfouzalsheikh
  slug: react-supervised-pilot
  name: React supervised pilot
  environment: local-staging
  max_concurrent_runs: 1
  environment_max_concurrent_runs: 1
tracker:
  # Product runs are explicitly approved and queued; no issue polling or publication.
  kind: memory
  active_states: [Todo]
  terminal_states: [Done]
workspace:
  root: /data/workspaces/react-supervised-pilot
hooks:
  environment:
    GITHUB_TOKEN: $GITHUB_TOKEN
  after_create: |
    set -eu
    test -n "$GITHUB_TOKEN"
    git -c credential.helper='!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
      clone --no-checkout https://github.com/mahfouzalsheikh/drawing-algorithms.git .
    git checkout --detach 5575c09854c10d45aa5cfdb2a9a6adec773b1da4
    test "$(git rev-parse HEAD)" = 5575c09854c10d45aa5cfdb2a9a6adec773b1da4
  before_run: |
    set -eu
    test "$(git remote get-url origin)" = https://github.com/mahfouzalsheikh/drawing-algorithms.git
    git merge-base --is-ancestor 5575c09854c10d45aa5cfdb2a9a6adec773b1da4 HEAD
  timeout_ms: 60000
agent:
  max_concurrent_agents: 1
  max_turns: 3
  max_tokens_per_run: 3000000
  max_retries: 0
validation:
  enabled: true
  policy: required
  required_checks:
    - id: reviewed-pilot-base
      name: Reviewed pilot source and whitespace
      command: >-
        git merge-base --is-ancestor 5575c09854c10d45aa5cfdb2a9a6adec773b1da4 HEAD
        && git diff --check 5575c09854c10d45aa5cfdb2a9a6adec773b1da4 HEAD
  max_attempts_per_run: 3
  command_timeout_ms: 1800000
review:
  enabled: false
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: externalSandbox
    networkAccess: restricted
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 120000
runtime_providers:
  codex:
    kind: codex
model_providers:
  default:
    kind: openai
tool_providers:
  candidate:
    kind: tempo
    allow_all: false
    tools: []
agents:
  pilot-planner:
    role: planner
    runtime: codex
    model: default
    completion: turn
    capabilities: [repository_write]
    tool_providers: [candidate]
    prompt: >-
      Follow the assigned product task and repository AGENTS.md. Confirm the reviewed source
      and write only the required design document. Return required decisions using Tempo's
      final JSON contract. Do not implement features or install dependencies.
  pilot-implementer:
    role: implementer
    runtime: codex
    model: default
    completion: turn
    capabilities: [repository_write]
    tool_providers: [candidate]
    prompt: >-
      Follow AGENTS.md and the assigned paths. Implement and test only this contribution,
      commit intended changes, and leave a clean checkout. No new dependencies, external
      services, publishing or deployment. Report concrete blockers. During an explicitly
      reviewed repair, follow the repair assignment and its allowed paths.
  pilot-integrator:
    role: integrator
    runtime: codex
    model: default
    completion: turn
    capabilities: [repository_write]
    tool_providers: [candidate]
    prompt: >-
      Inspect the imported contributions against the approved interfaces. Resolve integration
      defects and commit changes. For this controlled rehearsal, add exactly the specified
      compile-failure probe after integration. Leave that known probe for the separately
      reviewed repair; do not weaken build checks. Report all unrelated failures.
  pilot-verifier:
    role: verifier
    runtime: codex
    model: default
    completion: turn
    capabilities: []
    tool_providers: [candidate]
    prompt: >-
      Independently inspect the integrated source and report required decisions as final JSON.
      Do not edit or commit repository files. Distinguish implementation findings from the
      approved compile-failure probe. Do not claim browser acceptance or deployment readiness;
      Tempo owns those checks after repair.
workflow:
  name: supervised-product-candidate
  require_publication: false
  max_parallel_nodes: 2
  nodes:
    - id: candidate
      type: agent
      agent: pilot-implementer
  edges: []
---

Work only on the explicitly queued product assignment. Preserve the reviewed scope and source
identity. End with committed source and the declared task outputs. Do not publish or deploy.
