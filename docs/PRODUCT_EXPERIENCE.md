# Product experience: from idea to delivery

The UI redesign is part of building the software factory, starting in Phase 0. It should help an
operator answer three questions without reading agent logs: **What is happening? What needs me?
What happens next?** Backend safety work continues alongside these improvements.

## Findings in the current product

The original overview mixed six metrics, active runs, project counts, approvals, retries, and
diagnostics at similar prominence. Technical phase names appeared directly in run rows. Project
cards always said “configured,” including when a publication workflow lacked required checks.
Unavailable operator data could look like an empty inbox. There was no run search or project
filter, and live updates rebuilt open command details and keyboard focus.

Configuration has a second source of confusion: the JSON editor changes database-backed agents,
providers, and graph definitions, while tracker, hook, and validation settings come from workflow
files. The interface must explain that distinction until one coherent settings flow exists.

## Product structure

| Destination | Operator question | Default content |
| --- | --- | --- |
| Overview | What needs me now? | Next action, decisions, stopped work, active runs, project setup |
| Projects | What are we building? | Product brief, repository, environment, setup checklist, recent deliveries |
| Work | Where is this idea in the process? | Searchable run history, task ownership, dependencies, progress, next step |
| Decisions | What decision am I making? | Request, reason, scope, proposed effect, evidence, approve/reject/edit |
| Deliveries | Can this version be deployed? | Preview, acceptance results, immutable artifact, remaining gates, rollback |
| Settings | How should this project operate? | Agent roles, capabilities, validation, budgets, credential references |
| Operations | Is Tempo itself healthy? | Worker health, scheduler freshness, provider failures, capacity, diagnostics |

Introduce destinations as their backend capabilities become available. Do not add empty navigation
for product briefs, artifacts, or deployments that cannot yet be created or inspected. Preserve
existing URLs during migration; issue-to-PR delivery remains supported.

## Interaction rules

- Put the next useful action before secondary metrics. Give each blocker a reason and a practical
  next step. Refresh updates the view; explicitly named scheduler actions can trigger polling.
- Use plain states such as Building, Running checks, Reviewing, Waiting for a decision, and
  Stopped by a safety rule. Keep raw provider phases, thread IDs, and command output in details.
- Distinguish configuration from evidence. “Checks configured,” “Checks passed,” “PR created,”
  and “Deployment ready” describe different facts. Only the readiness evaluator can establish
  deployment readiness; neither a green run nor an agent message establishes it.
- Unknown, loading, empty, forbidden, stale, failed, and complete states need different messages.
  Never turn a failed request into a zero count. Show when data was received and stop exposing
  active controls when the necessary operator data cannot be refreshed.
- Show the requested effect before an approval. Make the normal path readable without raw JSON;
  retain an advanced editor for supported overrides. Keep destructive actions visually secondary.
- Keep search, expansion, scroll position, and keyboard focus stable during live updates. Announce
  connection changes, not every streamed token. Show stages and blocker reasons on small screens.
- Reuse existing templates, styles, and native accessible controls for this first pass. Extract
  reusable components as repeated flows emerge; a frontend framework migration is not a prerequisite.

## Delivery sequence and acceptance gates

| Slice | Timing | Deliverables | Acceptance |
| --- | --- | --- | --- |
| A. Understand current work | Phase 0, first implementation delivered | Action summary, plain status labels, run search/project filter, validation setup status, approval preview, stale-data handling, diagnostic disclosure | Find an active run; see a missing validation policy; inspect a request before approving; distinguish unavailable from empty; retain keyboard focus and open evidence on updates |
| B. Guided project setup | Phases 0–1 | Repository/environment selection, credential-reference checks, required-check setup, budget presets, readable settings forms with an advanced JSON escape hatch | A second operator connects a repository and identifies the exact missing setup step without editing arbitrary JSON; changes are validated and audited |
| C. Idea and run workspace | Phases 1–2 | Brief intake, acceptance criteria, reviewable plan, task list with dependencies and agent ownership, persistent run history and deep links | Trace one requirement to an assigned task and its evidence; recover interrupted work without reading scheduler internals |
| D. Decisions and recovery | Phases 1–3 | Dedicated inbox, human-readable effects, evidence links, explanations for blocked tasks, durable answers and scoped retry controls | Make an informed decision from one view; locate its audit record; resume the affected task without restarting unrelated successful work |
| E. Delivery workspace | Phase 3 | Preview, change summary, acceptance matrix, artifact identity, target-environment checklist, rollback instructions | Identify what passed, what failed, and why a release can or cannot be promoted for a specific environment |
| F. Usability and scale | Throughout, expanded in Phases 5–6 | Project permissions, durable read projections, pagination, saved filters, keyboard review, accessibility and operator task evaluations | Operators diagnose failure without logs by default; large histories stay usable; unauthorized data is unavailable server-side |

For setup, expose capability checks separately: repository access, credential resolution, required
validation policy, runtime availability, and isolated execution. Do not collapse these into a
single “healthy” badge. Do not expose secret values when reporting credential problems.

## First implementation and remaining work

Slice A includes an action-first overview, four primary counters, usage totals under System
details, active-work search/project filtering, and readable phases that remain visible on mobile.
Approval requests show a context preview before submission. Unknown operator counts display a
dash, errors explain the unavailable data, and controls disable until fresh operator data returns.
Run and nested command disclosures, plus keyed keyboard focus, survive a live update. Project
cards use actual validation configuration; the settings page explains which settings the editor
can change and lists required check names.

This is an initial redesign, not the completed product experience. Dedicated run pages and
history, a complete settings form, onboarding, product briefs, artifacts, and delivery readiness
still need their backend contracts. Read endpoints now require installation-wide authentication; project-level authorization remains
planned. Browser visibility and disabled controls do not replace server-side authorization.

## Verification

The deterministic browser scenario in `tests/browser/overview.cjs` exercises the real templates
and scripts against intercepted API responses. It covers search/filtering, setup blockers, nested
disclosures and focus after a streamed update, safe links and escaped request context, approval
preview/submission, failed state and operator requests, session expiration/recovery, read-only
access, and a 390px viewport. It never submits work or approvals to the real server.

Run `.venv/bin/python tests/browser/serve.py` for the loopback-only template fixture server.
This test host bypasses production authentication and never starts the scheduler. With Playwright and Chromium available, invoke the exported scenario:

```javascript
const { chromium } = require('playwright');
const check = require('./tests/browser/overview.cjs');
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    console.log(await check(page));
  } finally {
    await browser.close();
  }
})();
```

This scenario can also run through the Playwright MCP by passing the exported function to its
code runner. API failures are deliberately simulated, so associated HTTP error messages are
expected; uncaught page exceptions fail the scenario. Restart the local server after template
changes when Django's cached template loader is active. These fixtures supplement backend
authorization tests and visual inspection; they do not establish production usability or
replace evaluation with operators working on real repositories.
