# First live product-agent pilot

The [reviewed pilot contract](examples/mini-app-pilot.json) now has an implemented, checked,
locally published result. Five native Tempo agents added drawing tips and a resettable checklist
to the React mini-app. Two implementers ran concurrently in independent checkouts; integration
and verification followed. The [result receipt](examples/mini-app-pilot-result.json) records
source/build identities, reported tokens, task timing, and release evidence digests.

This was an **assisted pilot**. It establishes a working path from an approved enhancement brief
through real coding agents to a retained build and staging release. Operator build and environment
repairs were required. It does not establish fully autonomous recovery, new-repository bootstrap,
or completion of the wider software-factory plan.

## Result

- Live brief #1, plan #2, run #7: `CandidateChecksPassed`.
- Source base: `5b11540be95a83b9e200df82f02e214b42e7a501`.
- Final candidate: `5575c09854c10d45aa5cfdb2a9a6adec773b1da4`, pushed to
  [tempo/pilot-drawing-help-run-7](https://github.com/mahfouzalsheikh/drawing-algorithms/tree/tempo/pilot-drawing-help-run-7).
- Build #1: 37,947,227-byte ZIP, 26 files, 69,995,148 uncompressed bytes;
  SHA-256 `07e18e5b6bc0b6feacd339dd447d4da8f13c215549c39457eb49591a42c85a64`.
- Required validation #38: 27 application tests plus two asset-preparation regression tests passed;
  production build and distribution checks passed for seven prerendered pages.
- Acceptance #1: both criteria passed in fresh offline browsers. The checklist journey also
  uploaded the fixed PNG, generated Circle Shape Art, and downloaded a validated SVG with
  3,960 geometry elements. This regression extension was reviewed before acceptance execution.
- Preview health #1 and rollback rehearsal #1 passed. Release #1 published and was explicitly
  rolled back; the target returned HTTP 404. Release #2 republished the same artifact and is active.
- [Open local staging](http://5c96e222bd9e46cbb267bab9669934ad.localhost:8032/) on the Docker host,
  or inspect [the idea and its evidence](http://localhost:8001/ideas/1/).

The actual preview was checked at 390 and 1440 pixels. The help section fit the mobile viewport,
keyboard Enter opened the advice, and Space toggled a labelled checklist item. Both release
publications verified the root and every deployed file over HTTP. The staged page was opened
and its checklist exercised in a browser.

## Execution and interventions

The native tasks were planner, tips implementer, checklist implementer, integrator, and verifier.
Both implementation tasks started at 19:00:17 UTC on September 8, 2026. Their accepted-contribution
checkpoints were recorded at 19:02:17 and 19:02:18 UTC, establishing roughly two minutes of overlap.
The five nodes reported 1,489,526 total tokens, including provider-reported input/context usage;
this is not a dollar-cost calculation. Completed model work was reused during validation retries.

The first launch stopped when GitHub rejected the checkout credential. The user replaced the
private `GITHUB_TOKEN`, and Tempo was reloaded while preserving its pinned execution images.
The approved plan and saved execution snapshot were retained.

The planner's saved profile prohibited file edits, conflicting with the plan's requested design
document. It recorded useful interface and ownership decisions in node output, but did not create
that document. Plan/profile compatibility checks remain needed; a successful model turn does not
prove every task instruction was fulfilled.

After all agents finished, trusted preparation changed the tracked `social-preview.png` because
its SVG was rendered with the container's fonts. Tempo correctly rejected the dirty source. The
operator repaired the candidate's build script in a separate checkout: ordinary builds preserve
the reviewed PNG; maintainers explicitly refresh it when changing its SVG. Regression tests cover
preservation, refresh, and invalid existing images. Test-runner discovery and image-library caching
issues found during this repair were fixed too. The same stopped candidate was fast-forwarded to
the repair commits and retried through Tempo's normal checks. No build manifest or successful
validation result was inserted manually, and source-integrity checks stayed enabled.

A subsequent attempt rejected the newer validation runner installed by the intervening Tempo
release. With no other runs active, the runner was temporarily restored to the exact immutable
image in the saved snapshot. That mismatch remained recorded as validation #35; failed repair
checks #36 and #37 also remain in history. Validation #38 passed. Normal deployment restores the
current runner configuration after this completed candidate. A subsequent factory update now
supports concurrent validation with approved, locally retained image IDs; the original mismatch
and operator intervention remain part of this pilot's history.

## Factory fixes from this run

Run-level usage reporting previously replaced the graph total with whichever node emitted the
latest event. The fix sums node usage for running graphs, preserves individual session counters,
and uses the detached aggregate after graph completion/review so tokens are not counted twice.
Saved run totals, live totals, and cancellation accounting use the same calculation. Budget checks
already summed active graph usage; this change does not loosen budgets or alter saved contracts.

Replaying an already completed contribution also overwrote its finish time. Identical completion
replay now preserves that timestamp; a newly started attempt still receives a new completion time.
The pilot receipt uses original accepted-contribution checkpoints for implementer completion,
since this historical run encountered the old behavior before the fix. Existing rows are not
backfilled with invented times. Prior checkout errors are labelled as previous-attempt errors while
a retry is active, rather than presented as the current access blocker.

The Tempo full suite passed 617 tests with 22 environment-dependent skips before the final timing
fix. Focused persistence, orchestration, execution, and reporting checks cover that correction
(83 passed, two environment-dependent skips).
Django checks, migration consistency, and lint passed. Automatic product progress remains private,
revision-scoped, read-only, and respectful of keyboard focus; see [product execution](PRODUCT_EXECUTION.md).

Follow-up: [reviewed build repairs](PRODUCT_EXECUTION.md#reviewed-build-repairs) now give a stopped
candidate an explicit agent assignment with allowed paths and fresh required checks. Automated
regression tests exercise this path; this historical pilot remains an operator-assisted result.
The validation runner now also routes concurrent requests to exact image IDs collected from
verified saved contracts. Next priorities: detect plan/profile conflicts before spending model
tokens and repeat this benchmark without operator code repair before claiming autonomous delivery.
