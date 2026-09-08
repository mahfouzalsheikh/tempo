# First live product-agent pilot

The reviewed [pilot contract](examples/mini-app-pilot.json) adds drawing preparation help to the
existing React mini-app. It defines expandable photo/style tips, a resettable three-item
checklist, two independent implementation tasks, and browser journeys written before coding.
This is a bounded enhancement to an existing application, not a new repository bootstrap.

The dependency order is planner → two parallel implementers → integrator → verifier. Each
implementer has separate component/style/test ownership and an isolated checkout. Integration
remains serialized. Tempo must run the versioned mini-app build recipe, retain the resulting
artifact, and pass reviewed browser, preview-health, configuration, and rollback gates before
publishing to a named local staging target. The source checkout resolves the configured remote
at first execution; existing uncommitted local edits are outside this run.

## Current observation: repository access blocked

On September 8, 2026, the contract was saved as live brief #1, plan #2 and launched as run #7
with parallelism 2. GitHub rejected the configured HTTPS clone credential in `after_create`.
No task or model turn started, no candidate was accepted, and no artifact was created. The
run was explicitly stopped to avoid further automatic retries with the rejected credential.
Its plan, execution snapshot, and checkout failure remain available on the idea page.

The operator must replace `GITHUB_TOKEN` in the installation's private `.env` with a credential
that can read `mahfouzalsheikh/drawing-algorithms`, then reload the application environment.
Never put the credential in a brief, plan, report, Git commit, or chat. After access is restored,
use **Retry saved execution** on [the pilot idea](http://localhost:8001/ideas/1/).
Credential references remain in the saved snapshot; retry does not rewrite the reviewed plan.

This is evidence of an actual launch blocker, not evidence of successful agent execution.
The remaining demonstration is to run the saved graph, observe overlapping implementation
tasks, review their integrated source and host checks, run the saved browser journeys against
the retained artifact, publish it, and verify recovery. Record exact source/build/report IDs
and token usage when those stages have actually completed.

## Operator progress

Candidate builds now appear above the full brief and task plan. Pending work refreshes every
ten seconds while the tab is visible; focus and text selection defer replacement of the run
controls. A failed request retains the last displayed result and identifies interrupted updates.
Authentication redirects and mismatched plan fragments cannot replace the saved status.
Automatic polling stops when all displayed runs are stopped or finished; **Refresh status**
remains available, including without JavaScript. Revision links stay pinned to their selected
brief and plan. Checkout authentication errors explain the credential/reload/retry sequence.

The progress fragment is an authenticated GET with private/no-store caching and the same
product/plan scoping and server-rendered actions as the full page. Refresh never dispatches work.
Tests cover revision isolation, authentication, methods, escaping, and terminal status. The
browser fixture covers automatic updates, keyboard focus, failed/login/mismatched responses,
terminal polling, and absence of mutation requests.

Validation for this slice: 615 tests passed with 22 environment-dependent skips; the isolated
browser progress checks passed. Django checks, migration consistency, and lint passed. These
checks validate Tempo's changes, not the unexecuted mini-app enhancement.
