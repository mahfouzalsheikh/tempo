---
tracker:
  kind: github
  provider:
    # TODO: Replace with the GitHub repository Tempo should monitor.
    repo: mahfouzalsheikh/drawing-algorithms
    token: $GITHUB_TOKEN
  # TODO: Replace "tempo" if you want to use a different dispatch label.
  required_labels: [tempo]
  active_states: [open]
  terminal_states: [closed]
polling:
  interval_ms: 30000
workspace:
  root: $TEMPO_WORKSPACE_ROOT
hooks:
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
  max_tokens_per_run: 1000000
  max_retries: 2
  max_retry_backoff_ms: 300000
validation:
  enabled: true
  command_timeout_ms: 1800000
  cleanup_timeout_ms: 120000
  max_commands: 12
  max_attempts_per_run: 5
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 120000
---

You are working on {{ issue.identifier }}: {{ issue.title }}.

{% if issue.description %}{{ issue.description }}{% endif %}

Work autonomously in the current issue workspace. Follow the repository's own instructions and
tooling to build or launch the project and run its relevant tests locally. Use project_validation
with the complete project-native validation sequence. Fix failures and repeat validation as needed.
Only after validation passes, leave the workspace in a reviewable state, push the branch, and open
a pull request through available provider-native tooling. Never merge the pull request. If the
requested work is already present and the validated project needs no change, use the
tempo_complete tool with concrete evidence instead of repeating work.
