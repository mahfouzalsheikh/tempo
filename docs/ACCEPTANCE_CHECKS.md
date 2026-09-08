# Independent browser acceptance checks

Completed static builds now offer **Verify acceptance criteria**. The operator writes a
browser journey for every criterion in that build's saved brief, reviews the mapping, and
selects **Approve check plan**. **Run reviewed checks** queues durable work for a separate
worker. The page shows the latest result per criterion and preserves previous check plans
and run history. The idea page summarizes the latest reviewed plan's result.

This establishes the behavior exercised by the reviewed assertions. It does not establish
that arbitrary prose requirements have been completely tested. The operator must review the
coverage; a successful heading assertion is insufficient evidence for an upload requirement.
Automatic QA-agent proposals, broader browser actions, performance/API checks, and production
release readiness are subsequent work. The original build manifest remains immutable, including
its original unverified acceptance entries; browser evidence lives in separate records.

## A small, reviewable check language

The versioned `browser-acceptance-v1` contract accepts data, never shell commands or arbitrary
JavaScript. Every criterion needs 1–20 steps with at least one assertion; the whole plan has a
100-step limit. All criterion IDs must match the frozen brief exactly. Examples:

```text
open /
fill "Item name" "Example item"
click button "Add item"
expect text "Example item"
```

`open` accepts only local application paths. `fill` identifies a field by its exact label.
`click` and `expect` support accessible button, link, heading, textbox, checkbox, radio, tab,
and status roles with exact names. `expect text "Value"` requires visible exact text.
`click testid "id"` and `expect testid "id"` select an application's test ID;
`expect testid "id" "Exact text"` also checks its text. Ambiguous or missing elements fail.

The current language does not support uploads, downloads, external APIs, performance
assertions, or arbitrary scripting. Unsupported requirements remain outstanding. Each
criterion gets a clean context at the application root, with no inherited cookies or storage.
Assertions wait up to five seconds; the entire browser job has a two-minute watchdog.

Treat the published parser and report schema as versioned contracts. Extend them with a new
runner version when semantics change; do not silently reinterpret existing approved plans.

## Evidence and recovery

Migration `0018_acceptance_evidence` adds immutable reviewed `AcceptanceSuite` records and
durable `AcceptanceAttempt` records. Each attempt captures the ZIP, manifest, source commit,
brief, execution snapshot, reviewed checks, and immutable browser image identities. Approvals
use an expected current plan ID to reject concurrent overwrites. Artifact row locks serialize
operator submissions, and request keys make repeated submissions idempotent. Only one queued
or running acceptance attempt is allowed per artifact.

The worker claims queued work with a row lock and a unique lease. It revalidates the saved
candidate and required project-check evidence before dispatch. The image-owned harness
verifies its inputs and ZIP inventory, runs real Chromium actions, and emits a bounded report.
The host rejects missing criteria, changed actions, inconsistent statuses, partial passes,
incorrect digests, and incomplete output. Successful persistence requires confirmed container
and input cleanup, a current lease, unchanged artifact evidence, and a complete matching report.

Worker crashes, timeouts, runner errors, and invalid reports produce interrupted work, never
an inferred pass. Expired leases reject late results. Recovery confirms removal of the exact
attempt's container and private inputs before clearing interrupted work for a fresh attempt.
Cleanup failures keep that artifact blocked until recovery succeeds. Queued work survives
restarts; interrupted attempts require an explicit new run. Browser images are used by saved
immutable ID, so a missing old image produces interrupted work instead of silently substituting
the current image. A fresh attempt captures the currently configured image.

Newly approved checks require fresh evidence. A newer failed, interrupted, or incomplete run
does not inherit an older pass. UI reads revalidate completed evidence and do not display
tampered or stale results as passed. Operator pages remain authenticated, CSRF-protected for
mutations, and excluded from caches. Per-project authorization is still a broader platform gap.

## Execution boundary

`acceptance-worker` runs the trusted Django queue consumer, with database and execution-daemon
access but no model or tracker credential grants. It never runs the application's scripts.
Each job creates a private input directory beneath `/data/workspaces/.acceptance`, mounted
read-only as `/input` in a disposable container. No source checkout, sibling workspace,
database, Docker endpoint, or operator home is mounted into that container.

`Dockerfile.acceptance` pins Playwright Python 1.62.0 and its published base-image digest.
The deployment resolves and records the built image's immutable ID inside the execution daemon.
The official [Playwright Docker documentation](https://playwright.dev/python/docs/docker)
describes the browser image and Chromium sandbox options. This profile uses the offline Docker
container as its isolation boundary; Playwright's default Chromium launch disables the inner
Chromium sandbox. Defense against browser-engine exploits is a remaining hardening area.

The job has no network interface beyond loopback, a read-only root, private IPC/shared memory,
CPU/memory/process limits, a root watchdog, and an unprivileged browser/harness process. The
application runs from the retained ZIP through Tempo's static preview handler with the same
response policy as local previews. A local serving copy is created inside the job's temporary
filesystem. The live preview does not have to be started or renewed before running checks.
Browser routing also rejects external origins, and service workers are disabled. Browser
downloads are not accepted by this profile.

## Validation and deployment

`tests/test_acceptance.py` covers plan coverage, approval races, idempotency, authentication,
CSRF, evidence tampering, stale leases, recovery, cleanup failures, and the durable worker flow.
PostgreSQL tests exercise concurrent operator requests. A disposable UI/database rehearsal
ran the actual retained React mini-app through home-page and case-study navigation checks,
including persisted per-criterion results and a mobile operator view.

The deployment script builds and loads the acceptance image, drains the worker before
replacing execution infrastructure, starts it after Tempo's migrations/health check, and
runs `scripts/check-acceptance.py`. Its disposable fixture tests actual form entry, clicks,
fresh contexts, blocked external browser requests, and deliberately failing assertions.
It creates no production product, model call, or acceptance record. Existing backup behavior
preserves the database and all recorded check evidence. A heartbeat reports worker health.
