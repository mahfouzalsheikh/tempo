# Required validation policy

An enabled validation workflow defaults to `policy: required`. Its operator must configure at
least one required check in the control-plane workflow file. Publication workflows without checks
stop with `validation_policy_missing` before workspace preparation or agent launch. They enter a
safety stop instead of spending the agent retry budget. After configuring the policy, unblock or
requeue the run.

Required commands are selected by configuration, not by `project_validation` arguments. The tool
runs them first in configured order, then any supplemental commands supplied by the agent. It
stops the sequence on failure, always attempts cleanup, and only reports success when every
executed command and cleanup succeeds. Required checks that follow a failure remain unexecuted;
the attempt cannot pass. `max_commands` limits supplemental commands; policy permits up to 50
required checks independently.

## Configuration example

For a project building **Tempo itself**, with its development dependencies installed in `.venv`:

```yaml
validation:
  enabled: true
  policy: required
  required_checks:
    - id: lint
      name: Python lint
      command: .venv/bin/ruff check .
      timeout_ms: 120000
    - id: tests
      name: Regression suite
      command: .venv/bin/pytest -q
    - id: django
      name: Django system checks
      command: .venv/bin/python manage.py check
    - id: migrations
      name: Migration consistency
      command: .venv/bin/python manage.py makemigrations --check --dry-run
  command_timeout_ms: 1800000
  cleanup_timeout_ms: 120000
  max_commands: 12
```

Choose commands appropriate to each target project. The checked-in `WORKFLOW.md` targets another
repository and deliberately leaves its required checks empty; Tempo's checks must not be applied
to that project by assumption. Configuration is read from the host workflow, outside the agent's
issue checkout. Validation policy is not part of the database-backed graph/model/tool editor.

Each check requires a unique ID, a nonblank name and command, and an optional positive timeout.
Unknown configuration fields and duplicate IDs are rejected. A policy-level `cleanup_command`
is optional and always runs last, after any agent cleanup. The agent cannot override it.

With the policy above, this tool call runs all four checks:

```json
{"summary": "Verify the candidate against the project policy"}
```

The agent can include `commands: [{"name": "Focused regression", "command": "..."}]` to run
additional checks. Reusing a required check's name does not replace that check. Arguments that
attempt to set policy fields or check IDs are rejected. Clean-candidate and unchanged-workspace
requirements still apply at the publication boundary.

## Evidence and recovery

Validation attempts record a digest of the full validation configuration and effective runner
endpoint, plus the required check IDs. Individual command records include their host-assigned
check ID. These fields are available in Django's read-only runtime administration. Fingerprint
checkpoints link to the specific validation record; identical source fingerprints from different
attempts do not mix their node identities.

A policy change during execution prevents that result from passing. On resumption, the current
source fingerprint and policy digest must both match before publication authority is restored.
A newer failed, running, or invalidated attempt prevents fallback to an older pass. Changed policy
routes retained graph work back through verification. Review approvals also record the policy
digest; an approval made under another policy requires a fresh review after restart.

Remote runner responses must contain one terminal result with an integer exit code and text
output. Missing/duplicate results, Boolean or string exit codes, unexpected stream messages, and
messages after a terminal result are rejected. A total request deadline bounds streaming jobs in
addition to the runner's own command timeout.

## Upgrade and limits

Apply migration `0010_validation_policy_evidence` before starting the updated service. Historical
rows have no policy identity; their evidence cannot restore publication authority. Add project
checks, revalidate retained work, and unblock affected runs. For a deliberate temporary migration,
`policy: discovered` preserves agent-selected command sequences; it still runs any configured
required checks, requires at least one command overall, and treats cleanup failure as a failed
attempt. `enabled: false` remains an explicit operator policy choice. These compatibility choices
do not provide the required-check guarantee of a configured required policy.

This enforces the selected command set and binds its evidence to a policy. It does **not** make
repository scripts or tests immutable: an agent can still edit files those commands execute.
Ignored build products remain outside the source fingerprint. Trusted external test harnesses,
clean build sandboxes, runner authentication, secret isolation, and report/acceptance validation
remain separate work before this becomes a complete deployment-readiness gate.
