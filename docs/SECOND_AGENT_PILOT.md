# Second supervised React pilot: review package

Prepared 2026-09-10. Status: draft awaiting review; no claim of a completed second delivery.
The [machine-readable package](examples/mini-app-second-pilot.json) contains the exact brief,
version 2 plan, profile bindings, browser checks, repair scope and proposed staging target.

The feature adds remaining-step feedback to the preparation checklist and group expand/collapse
controls to the existing drawing tips. Preserve the original image upload, SVG generation and
download journey. No dependencies, persistence, analytics, accounts or backend changes are included.
The new account-management product work remains tracked in [the account plan](AGENT_ACCOUNTS.md).

## Execution setup

The dedicated [pilot workflow](../workflows/REACT_PILOT.md) creates a separate
`mahfouzalsheikh/react-supervised-pilot` project. It uses the memory tracker, so it has no issue
polling source and cannot pick up unrelated GitHub issues. The existing workflow remains loaded.
Checkout pins `5575c09854c10d45aa5cfdb2a9a6adec773b1da4`, the first pilot's retained source,
and checks that exact initial HEAD. Before-run and required validation checks enforce ancestry.
Agent profiles and instructions are specific to this pilot; no generic read-only planner is
assigned a writing task. The verifier returns structured decisions without modifying source.

| Task | Work and required outputs |
| --- | --- |
| Design | Inspect the pinned source; commit a design document; return interface and ownership decisions |
| Remaining steps | Update checklist feedback and its component tests in an isolated checkout |
| Group tips | Update disclosure controls and their component tests in a separate concurrent checkout |
| Integrate | Inspect the combined work; commit the controlled compilation probe; record integration findings |
| Verify | Read-only independent review with explicit findings for every criterion and the expected build failure |

The two implementers may overlap; shared-checkout tasks run serially. The saved workflow limits
one active run, no automatic run retries, three turns per task, and three host check attempts.
The 3,000,000-token limit is per execution attempt. One reviewed repair may start another attempt,
so the combined authorized ceiling for this pilot is 6,000,000 reported tokens. This is not a
dollar budget. The platform's general repair mechanism permits more requests; this pilot's
review package limits the operator to one. Any broader recovery requires a new review.

## Controlled failure and repair

The integrator deliberately commits `mini-app/src/pilot-repair-probe.ts` containing:

```typescript
export const pilotRepairProbe: string = 1;
```

The baseline's TypeScript configuration includes `src`, so the production build must reject
this mismatch. The file is never imported by the application. A host validation failure is
required evidence for this experiment; the verifier must identify it as expected and report
any unrelated problem separately. A successful first build means the fault-injection part of
the experiment was not demonstrated.

After that failure, review a repair allowing only this file. The native repair agent deletes
it and commits the deletion; the operator does not edit source. The controller must then rerun
the original mandatory checks and retain a new build. Do not change tests, TypeScript settings,
dependencies or build scripts to bypass the fault. Preserve the failed check, repair request,
commit and fresh-check evidence. This demonstrates a controlled, operator-reviewed repair;
it is not evidence of autonomous diagnosis or general recovery from arbitrary defects.

## Acceptance and release gates

The prepared browser plan has 35 steps across three criteria:

- Correct remaining counts, completion feedback, reset and reload behavior.
- Group and individual drawing-tip transitions with the original advice visible when open.
- The existing fixed PNG upload, Circle Shape Art generation and byte-validated SVG download.

The current acceptance language cannot assert hidden elements or ARIA attributes directly.
Focused component tests and independent source review cover those details; the browser plan
checks the visible outcomes and state counts. Also inspect the actual preview at 390px and a
desktop width for layout/focus regressions before approving release readiness.

Use a new local staging target named `preparation-feedback-pilot`. Do not replace the first
pilot's target. Browser check approval, preview health, target configuration, rollback rehearsal
and publication still use their normal review/evidence gates. Publication requires a separate
decision once the exact artifact and target are reviewable.

Record every approval, repair request, environment intervention and source edit. Record failed
attempts, task overlap, model tokens, elapsed time and available cost information. Never relabel
the first assisted pilot as unassisted or infer repeatability from this single additional run.

## Next action

After deploying the dedicated workflow, import the package with
`scripts/prepare-second-pilot.py` inside the Tempo container, passing the copied JSON file path.
The importer validates the actual saved project configuration, records the draft under the
configured operator, and is idempotent for the unchanged package. It never records approval,
queues agents, or overwrites an operator-edited pilot plan.

Preparation validation: 24 package/workflow/foundation tests passed, including idempotent draft
creation and refusal to overwrite operator revisions. Ruff and Compose configuration checks
passed. The deployed isolated hook cloned the repository, checked out the exact reviewed SHA,
passed its ancestry check, and removed the temporary preflight checkout. No model turn ran.

Review the draft brief and version 2 plan in **Ideas & plans**, then approve the exact revision
before starting the candidate. [Product execution](PRODUCT_EXECUTION.md) requires the latest
approved plan and its exact digest; draft preparation does not authorize dispatch. The same
package provides a reviewable repair scope and browser plan for their later explicit gates.
