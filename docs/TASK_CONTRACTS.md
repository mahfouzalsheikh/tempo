# Checked task contracts

Version 2 product plans declare task requirements and required outputs. Existing version 1
plans, starter templates and saved runs retain their original serialization and behavior.
Enable **Enforce task requirements and deliverables** in the plan editor, review every task,
then save and approve the new revision. This is an explicit opt-in for the next pilot.

Each version 2 task adds three required fields (empty lists are allowed):

```json
{
  "requires": ["repository_write"],
  "required_files": ["docs/design.md"],
  "required_decisions": ["interface-choice"]
}
```

`requires` currently accepts only `repository_write`. Tasks with required files must declare
it. Assigned profiles must include it in their existing `capabilities` list, and Codex runtime
settings must permit writing. Tempo checks the saved configuration before queueing the run.
Declare capabilities according to the profile's actual instructions and runtime authority;
Tempo does not infer contradictions from arbitrary prompt prose. Capability declarations are
compatibility metadata, not a substitute for a sandbox.

A task without `repository_write` must finish with a clean repository at its starting commit.
The assignment base is checkpointed before launch and preserved on retry, so an unauthorized
edit cannot become an accepted baseline simply by retrying the task.
For each required file, the controller checks the clean task commit for a nonempty regular
file at the exact path and records the commit, Git blob identity, and size. Uncommitted files,
directories, symlinks, empty files, and missing paths cannot satisfy the contract. An existing
committed file may satisfy an existence requirement; the content's correctness still requires
independent verification. Task-scoped evidence does not prove that later tasks preserve a file.

A task with required decisions must end with this JSON message, without Markdown fences:

```json
{
  "decisions": [
    {
      "id": "interface-choice",
      "decision": "Keep the existing component interface.",
      "rationale": "The enhancement can use its current inputs."
    }
  ]
}
```

Exactly one entry is required for each declared decision ID. The host reads the complete final
message from Codex `item/completed` agent messages or an external bridge's
`assistantMessage/completed` event, not the truncated activity display. Unsupported or missing
result messages block completion. Decisions and file identities are retained in node output
under `deliverables`; acceptance checks remain independently owned by Tempo.
The idea's run view exposes these records in each node's **Task deliverables** disclosure.

Missing deliverables stop the task before accepting its contribution or unlocking dependants.
Version 1 repairs keep their existing reviewed repair contract. General typed findings, semantic
document checks, automated plan discovery, and a live version 2 pilot remain future work.

Local validation on 2026-09-10: the full suite passed 689 tests with 24 environment-dependent
skips. After the final retry-baseline and evidence-view changes, the affected task, execution,
intake and progress suites passed 89 tests with four PostgreSQL skips. Ruff, Django system checks,
and the migration drift check passed. This records local regression evidence, not deployment
or a live model-agent pilot.
