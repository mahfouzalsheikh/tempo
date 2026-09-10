# Second supervised React pilot: result and review package

Executed 2026-09-10. Status: accepted candidate with staging readiness demonstrated;
publication awaits the separate artifact-and-target decision.
The [machine-readable package](examples/mini-app-second-pilot.json) contains the exact brief,
version 2 plan, profile bindings, browser checks, repair scope and proposed staging target.
The [result receipt](examples/mini-app-second-pilot-result.json) records the actual evidence.

## Observed result

Brief **2**, approved plan **4**, run **8**, build **2** completed through native Tempo agents
without operator application-source edits or environment repairs. The two implementers overlapped
for **90.20 seconds**. Approval to checked candidate took **587.66 seconds**, including the
controlled failure, operator review and one native repair. Reported model usage was **1,481,164
tokens**: 1,287,721 initially and 193,443 for repair. Dollar cost is unknown.

The planner committed its required design document and returned both required decisions. All five
original tasks passed their version 2 completion contracts; the read-only verifier retained four
structured findings and changed no source. Its source review predates repair, so it does not claim
post-repair browser acceptance. The integrator's local test/build commands lacked dependencies;
Tempo's normal build preparation installed the locked dependencies successfully.

Validation **39** passed ancestry/whitespace checks, **31 application tests** and **two asset tests**,
then failed only on the intended `TS2322` in `pilot-repair-probe.ts`. The operator submitted the
previously reviewed one-file assignment through the live controller. The native repair deleted
only that probe and committed `d890ae249234aa8ea5f69f5655ce48b665c747df`. Validation **40** reran
all required checks and passed, including production build and distribution validation. No tests,
dependencies, configuration or build commands were weakened.

Build **2** is a 37,947,513-byte ZIP containing 26 files, with SHA-256
`c85b28a03d8fe043110432ef754ee3b27ae2feb60fd31bbe8e07b44181cd3246`.
Its native-agent source commits are pushed to
[`tempo/preparation-feedback-run-8`](https://github.com/mahfouzalsheikh/drawing-algorithms/tree/tempo/preparation-feedback-run-8).
Browser suite **2**, attempt **2**, passed all **35 steps across three criteria**, including the
fixed PNG upload and a byte-validated SVG download with 3,960 geometry elements.

Independent Chromium checks at 390px and 1440px verified native Enter/Space activation,
`aria-expanded`/`aria-controls`, hidden panels, checkbox operation, a visible 3px focus outline and
no horizontal overflow. Visual review found no layout regression in the changed help section:
[mobile](evidence/second-pilot/second-pilot-mobile.png),
[desktop](evidence/second-pilot/second-pilot-desktop.png).
There were no application console errors; Chromium emitted the existing preview sandbox-policy
warning about combining `allow-scripts` and `allow-same-origin`.

The isolated rollback rehearsal **2** and preview health **2** passed. All six readiness gates
passed at the receipt's evaluation time. This new target had an empty baseline, so rehearsal
verified temporary activation, withdrawal to HTTP 404, and cleanup. No live publication or public
rollback has occurred. Preview health expires after five minutes and must be refreshed before
publication. The first pilot's target remains active.

The operator recorded approvals and dispatched work under the existing operator identity based on
the user's instruction to continue the prepared plan. One control-helper script failed on JWT tuple
handling before sending any request, then was corrected; there was exactly one repair dispatch.
The receipt records these actions separately from agent work. This is a supervised, deliberately
seeded repair of an existing application. It does not establish autonomous diagnosis, multi-account
execution or a repeatable delivery rate. All six model sessions used the existing Codex login.

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

## Draft preparation (historical)

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

## Next action

Review [the accepted candidate and evidence](http://localhost:8001/ideas/2/) and
[the temporary preview](http://b0369a99984f48ae9ba84c1317089fd5.localhost:8031/).
The prepared static target is `preparation-feedback-pilot`, configuration **2**, at
`http://8d5656972aa341979b05fe40d64946f6.localhost:8032/`. Publishing this exact artifact remains a
separate decision; the URL is reserved and does not yet serve the candidate.

The next implementation slice is the named Codex account registry and project/profile selection
in [the account plan](AGENT_ACCOUNTS.md), alongside repeat-run reporting and CI. Native Claude
support and account-isolation acceptance remain open.
