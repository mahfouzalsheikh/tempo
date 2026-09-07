# Product briefs and reviewable task plans

Open **Ideas & plans → New idea**. Choose an existing project and describe the goal, users,
scope, exclusions, constraints, and target. Add observable acceptance criteria with a verification
method for each. Saving creates a brief revision and an editable starter task plan.

The starter generator is a deterministic template, not an agent that has inspected the
repository or designed the application. It creates a planning task, one implementation task per
criterion, an integration task, and independent verification. Dependency groups make potential
parallelism visible. Operators must review shared interfaces, file ownership, task granularity,
and prerequisites against the actual repository before execution setup.

The plan editor accepts task titles, roles, instructions, criterion IDs, and prerequisites without
requiring JSON. Validation rejects duplicate IDs, unknown criteria or prerequisites, cycles,
uncovered criteria, and verification that precedes the corresponding implementations. Each
criterion needs implementation and verification coverage. These are structural planning checks;
they do not prove that requirements are complete, tests are sufficient, or the code works.

## Revisions and approval

`ProductBrief` belongs to a project. `BriefRevision` saves a schema-versioned specification and
its canonical SHA-256 digest. `ExecutionPlan` saves its exact brief digest, typed task contracts,
plan digest, author, generator identity, and revision number. Plan approval records the operator
and timestamp for that exact revision. These records are read-only in Django administration;
supported edits append new revisions.

Changing a brief creates a new starter plan without carrying forward the previous plan's
approval. Editing a plan creates a new unapproved plan revision for the same brief. Historical
briefs, task plans, and approvals remain accessible from revision history. Criterion IDs are
retained when editing a criterion; its meaning is scoped by the immutable brief revision.

Unresolved questions prevent approval. Approval checks the saved brief and plan digests and
revalidates task references and ordering. It does not queue coding agents or assert deployment
readiness. There is no agent attempt or release evidence attached to these plans yet.

Mutations lock the product row. Expected revision or plan identity rejects stale edits and
approvals. Initial creation uses a UUID submission key, serialized within the project, to avoid
duplicate briefs when the same request is repeated. Reusing that key with different content,
project, or operator is rejected. Project activity is required for writes. Authentication follows
Tempo's existing installation-wide operator model; project roles remain future work.

## API

All endpoints require authentication. Browser forms and cookie-authenticated API mutations
require CSRF protection. Valid bearer-token requests use the existing API authentication path.

| Method and endpoint | Request / behavior |
| --- | --- |
| `GET /api/v1/briefs` | Latest 100 briefs and active projects |
| `POST /api/v1/briefs` | `project_id`, UUID `request_key`, and `brief` contract |
| `GET /api/v1/briefs/{id}` | Current brief, plan, identities, approval, and revision history |
| `GET /api/v1/briefs/{id}?revision=N&plan=M` | Inspect historical brief and plan contracts |
| `POST /api/v1/briefs/{id}` | `expected_revision` and replacement `brief` contract |
| `POST /api/v1/briefs/{id}/plans` | `expected_plan_id` and replacement `plan` contract |
| `POST /api/v1/briefs/{id}/approve` | `expected_plan_id` and `expected_digest` |

The schemas live in `tempo/contracts/intake.py`. A brief includes `title`, `goal`, `users`,
`scope`, `target`, and 1–30 criteria with `id`, `outcome`, and `verification`. Exclusions,
constraints, and open questions are optional text. A plan includes `brief_digest` and 1–50 tasks,
each with an ID, title, role, instructions, criterion references, and prerequisite IDs. Allowed
roles are planner, implementer, integrator, and verifier. These roles do not provision agent
profiles or grant tools. Contract schema version is 1; unknown fields are rejected.

## What follows

Connect an approved plan to task-based execution with pinned project/workflow settings,
repository identity, agent assignments, tool grants, and enforceable work packages. Add an
agent planner that inspects the repository and proposes these same validated contracts. Track
results and acceptance evidence against the approved brief/plan identities, then build the
supported application-to-preview release slice. Numeric budgets, project onboarding templates,
automatic scope-change impact analysis, artifact storage, and release readiness remain future
work; no existing result is reused across a changed brief by this intake implementation.

Tests cover structural contracts, immutable history, stale edits, approval integrity, unresolved
questions, authentication, CSRF, submission replay, and PostgreSQL edit races. Browser checks
exercise create → edit plan → approve → revise brief on desktop and mobile. Deployment checks
verify migrations, stored contracts, cycle rejection, and protected reads without creating work.
