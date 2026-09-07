# React mini-app builds and retained artifacts

The first supported build target is the React application under `mini-app/` in the configured
Drawing Algorithms repository. Open **Ideas & plans**, review and approve a plan scoped to that
application, then choose **Set up candidate build → React mini-app**. Review the combined required
checks and agent assignments before starting. Parallelism remains bounded by the project settings shown in setup. The setup default
remains one, and integration stays exclusive.

The recipe adds the mini-app's unit tests, production build, and distribution checks to any
existing required project checks. It can supply the missing check list for a product run, but
cannot override disabled validation, discovery policy, or applicable human gates. An issue-label
gate is inactive only when every incoming condition is provably false for the product run's
saved labels. Setup shows that decision; unconditional, unknown, or matching gates block launch. It does not
change the legacy issue workflow's validation policy or automatically start GitHub work.

## Build boundary

After agents and lifecycle hooks stop, Tempo prepares locked npm dependencies and model assets
in a disposable container with public network access. This preparation has no tracker, model,
validation-runner, or Docker credentials and no imported agent home. The recipe skips optional
ONNX CUDA downloads because this target runs in a browser. Runtime images include the fonts
needed to generate the app's social preview image.

Tempo checks that preparation preserved the candidate's clean source identity, rejects tracked
build output, and clears stale `mini-app/dist` through directory file descriptors. It then runs
the complete saved check list through the existing offline validation containers. The build
command is `npm run build && npm run validate:dist`; the repository's build script prepares its
assets, compiles TypeScript, bundles the app, and prerenders pages.

After all check commands and their containers finish, a host callback captures the distribution
files before policy cleanup. Failed capture, failed cleanup, changed source, or lost ownership
prevents acceptance. Cleanup may remove generated files without affecting the captured bytes.
Build preparation has its own timeout; quiet preparation is not treated as a stalled agent.
Agents cannot submit accepted build events or artifact records.

## Retention and downloads

Migration `0016` adds `BuildArtifact`. The controller stores the ZIP bytes, SHA-256, size, evidence
manifest, and manifest digest in PostgreSQL through a lease-fenced write. The subsequent candidate
checkpoint references the exact artifact. An interrupted write cannot expose a successful build
without matching candidate and validation evidence. Repeated writes for the same manifest are
idempotent. Existing product snapshot schema 1 remains supported; new build runs use schema 2
with a saved recipe. Published recipe IDs must remain unchanged; revisions need a new ID.

The archive uses deterministic entry metadata and records each file's path, size, and SHA-256.
Capture rejects symlinks, hardlinks, special files, unsafe paths, missing `index.html`, and oversized
outputs. Both uncompressed contents and the ZIP are limited to 128 MiB, with at most 10,000 files.
The database backup includes artifact bytes. Object storage, retention policies, streaming very
large downloads, and storage quotas remain later operational work.

Successful plan pages offer **Download build ZIP** and **Download evidence manifest**. Downloads
require operator authentication, verify bytes and evidence identities, and use attachment,
`nosniff`, and sandbox headers. Tempo does not execute or serve the application's HTML on its
operator origin. Download remains possible after the original workspace has been removed.

The manifest identifies the source commit, brief, plan, execution snapshot, recipe, execution
images, validation record, policy, required checks, archive, and file inventory. Acceptance
criteria are explicitly `unverified`, and `deployment_ready` remains false. Passing repository
scripts does not independently establish each product requirement or prove that the scripts
are sufficient. A preview deployment, independent acceptance harness, and promotion/rollback
checks are the next release stages.

## API and verification

Pass `build_target: "react-mini-app-v1"` to `POST /api/v1/briefs/{id}/execute`, alongside the existing
reviewed plan/configuration identities and role bindings. The execution setup endpoint accepts
`?build_target=react-mini-app-v1` to show the effective check list. Historical source-only runs
retain their original execution contracts and do not gain an artifact on retry.

`GET /ideas/{brief}/runs/{run}/artifacts/{artifact}/archive/` downloads the ZIP;
replace `archive` with `manifest` for JSON. Both endpoints require matching product, run,
candidate checkpoint, and passed validation evidence.

Tests exercise graph-to-artifact execution, snapshot compatibility, failed checks and cleanup,
unsafe output, source mutation, stale leases, replay, protected downloads, corruption, and
workspace removal. The deployment probe runs the real preparation and offline build machinery
on a disposable dependency-free fixture and verifies retained artifacts without creating runs.

A separate container rehearsal used committed mini-app source `5b11540`: 18 unit tests passed,
production validation accepted seven prerendered pages and 26 files (66.7 MiB), and capture produced
a 37,945,194-byte ZIP with SHA-256
`61d29496dd52f235587c6536df199fda74495b465d8728e5b287a74b2c5f4fd3`.
This verifies the supported build recipe on existing source; it is not a generated-product
acceptance run or a deployment of the application. Local uncommitted mini-app edits were excluded.
