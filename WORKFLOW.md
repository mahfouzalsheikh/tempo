---
project:
  organization: mahfouzalsheikh
  slug: drawing-algorithms
  name: Drawing Algorithms
  environment: development
  max_concurrent_runs: 3
  environment_max_concurrent_runs: 3
tracker:
  kind: github
  provider:
    # TODO: Replace with the GitHub repository Tempo should monitor.
    repo: mahfouzalsheikh/drawing-algorithms
    token: $GITHUB_TOKEN
    # Optional separate identity for a formal GitHub APPROVE review.
    review_token: $GITHUB_REVIEW_TOKEN
  # TODO: Replace "tempo" if you want to use a different dispatch label.
  required_labels: [tempo]
  active_states: [open]
  terminal_states: [closed]
polling:
  interval_ms: 30000
workspace:
  root: $TEMPO_WORKSPACE_ROOT
hooks:
  environment:
    GITHUB_TOKEN: $GITHUB_TOKEN
  after_create: |
    test -n "$GITHUB_TOKEN" || {
      echo "GITHUB_TOKEN is required to clone the configured repository" >&2
      exit 1
    }
    git -c credential.helper='!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
      clone https://github.com/mahfouzalsheikh/drawing-algorithms.git .
  before_run: |
    test -n "$GITHUB_TOKEN" || {
      echo "GITHUB_TOKEN is required to fetch the configured repository" >&2
      exit 1
    }
    git -c credential.helper='!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
      fetch origin
  timeout_ms: 60000
agent:
  max_concurrent_agents: 3
  max_turns: 6
  # This budget is cumulative across every agent node in one execution attempt.
  # Lifetime usage remains visible, but a durable retry starts a fresh bounded
  # budget for unfinished work.
  max_tokens_per_run: 3000000
  max_retries: 2
  max_retry_backoff_ms: 300000
validation:
  enabled: true
  policy: required
  # Configure this project's mandatory checks before enabling dispatch.
  # See docs/VALIDATION_POLICY.md for a complete Tempo-repository example.
  required_checks: []
  command_timeout_ms: 1800000
  cleanup_timeout_ms: 120000
  max_commands: 12
  max_attempts_per_run: 5
review:
  enabled: true
  max_turns: 3
  auto_merge: true
  merge_method: squash
  # Notified when Tempo or repository policy requires a person.
  reviewers: [mahfouzalsheikh]
  team_reviewers: []
  prompt: |
    Independently review the issue, complete pull-request diff, and repository guidance. Check
    correctness, regressions, security, tests, and maintainability. Fix material findings when
    safe, then rerun the complete diff-relevant project validation sequence and update the
    pull-request branch. For frontend-only changes, do not add unrelated backend checks or test
    modules. For backend changes, install the locked Pipenv dependencies inside the isolated
    validation sequence before Django checks and focused tests. Approve only with concrete
    evidence. Request human review for sensitive changes, ambiguous requirements, unresolved
    findings, or repository policies that require a person.
codex:
  command: codex app-server
  # When authenticating with an API key, explicitly grant it to this runtime:
  # environment:
  #   OPENAI_API_KEY: $OPENAI_API_KEY
  approval_policy: never
  thread_sandbox: workspace-write
  # Docker is the execution boundary. Nested Linux bwrap namespaces are not
  # available under the container's default AppArmor profile, so tell App
  # Server not to create a second sandbox layer for turn commands.
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
    # Omit model to use the runtime default. Add routes and fallbacks for role-aware routing.
tool_providers:
  engineering:
    kind: tempo
    tools: [github_api, github_publish, github_comment, project_validation, tempo_complete]
agents:
  implementer:
    role: implementer
    runtime: codex
    model: default
    tool_providers: [engineering]
    completion: publication
workflow:
  name: issue-to-pull-request
  max_parallel_nodes: 1
  require_publication: true
  nodes:
    - id: implementation
      type: agent
      agent: implementer
  edges: []
---

You are working on {{ issue.identifier }}: {{ issue.title }}.

{% if issue.description %}{{ issue.description }}{% endif %}

Work autonomously in the current issue workspace. Follow the repository's own instructions and
tooling to build or launch the project and run its relevant tests locally. Use project_validation
to run the required policy checks, adding focused supplemental checks when needed. In discovery
mode, supply the complete project-native sequence. Fix failures and repeat validation as needed.
Commit your changes, validate them, then use github_publish with a title and body. Tempo publishes
the exact commit to its run branch and opens or recovers the pull request. Use github_api for reads
and github_comment for source-issue updates. Do not merge it yourself: Tempo starts a
separate review agent and applies the configured merge policy after that review. If the requested
work is already present and the validated project needs no change, use the tempo_complete tool with
concrete evidence instead of repeating work.
