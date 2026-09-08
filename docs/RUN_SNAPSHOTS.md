# Execution configuration snapshots

New runs save their execution inputs when queued. Reloading a workflow file or editing agents in
the configuration screen affects future runs. Retries, resumed sessions, graph recovery, review,
publication checks, hooks, and worker safety limits use the run's saved configuration.

## What is captured

Each run holds an independent JSON snapshot with a schema version and SHA-256 digest. It includes:

- The complete parsed `ServiceConfig`, including defaults, database-managed agent/provider/graph
  sections, tracker target and routing rules, workspace root, hooks, budgets, retries, required
  validation checks, and review/merge policy.
- The main workflow prompt and its source path. Execution does not reread that path on retry.
- The installation's runtime backend, runtime image/default validation image, runtime watchdog,
  private agent-state root, validation backend, and validation runner URL.
- The portable model settings used to seed Docker runtime homes. Ambient connectors, authentication
  files, and arbitrary host settings are not imported.

Credential references remain references. Actual credentials are resolved at their existing
consumer boundaries, so rotation and revocation still apply. Snapshot capture does not copy values
from credential environment variables or read login caches. Operator-supplied prompts, commands,
and arbitrary provider settings remain configuration content; this is not a general secret scanner.

The workflow version checksum now covers the complete snapshot, including the main prompt and
non-platform settings. Execution defaults are captured when a configuration version is created
at startup or reload. Version allocation locks the project row. Runs copy the snapshot at enqueue
time rather than depending on mutable catalog rows. Run-node definitions are selected from the
run's original workflow version. The current provider catalog remains useful for configuration;
it is not the source of truth for an existing run.

Before execution, Tempo verifies the digest and schema, reconstructs independent configuration
objects, and binds persistence and tracker resources to that run. Task-local execution settings
also propagate to its child tasks and helper threads. Concurrent runs cannot change each other's
environment defaults by mutating the process environment.

The dashboard shows the pinned configuration digest on active runs. The configuration editor
explains that changes apply to newly queued work. Runtime records expose the snapshot and digest
read-only in Django administration.

## What remains live

Current project/environment concurrency limits control admission. Polling frequency, operator
pause/cancel/feedback actions, tracker issue state, credential validity, network restrictions,
and infrastructure availability remain live. Tracker reconciliation uses each active run's saved
target and routing rules. Durable retries are first identified from their saved issue record,
then refreshed through their own tracker before hooks or coding agents launch.

Changing a token budget or retry policy does not rewrite an existing run. Pause or cancel that
run if it must stop. A resumed attempt retains the established per-attempt budget accounting;
this change does not introduce a lifetime dollar budget.

## Upgrade and failure behavior

Migration `0012_execution_snapshots` adds snapshot fields without modifying historical records.
Old workflow records did not retain the main prompt or all runtime defaults, so Tempo cannot
reconstruct their original execution contract reliably. Historical completed records remain
readable. An older queued, paused, or retried run without a complete snapshot stops with
`snapshot_missing` before a tracker or workspace hook is launched. It does not adopt the latest
configuration. Ordinary retry/requeue is not a configuration migration and cannot repair this.
Use the explicit [fresh restart operation](RUN_RESTARTS.md) to create a successor under the
current configuration. Do not fabricate historical snapshots or reuse old validation approvals
under a new policy. In-place schema migration remains future work.

Changed, malformed, or unsupported snapshots stop with `snapshot_invalid`. Configuration schema
changes that add implicit defaults require an explicit snapshot migration instead of silent
reinterpretation. Editing a saved workflow/catalog row does not alter a run's independent copy.
The digest detects accidental changes; it is not a signature protecting against an administrator
who can rewrite both a snapshot and its digest directly in the database.

Execution backend changes stop with `snapshot_environment_changed`. A Docker run cannot fall back
to host validation when its saved runner URL is absent. Runtime image IDs remain pinned and must
still exist in the execution daemon. The remote validation runner accepts its current image and
retained IDs collected from verified saved snapshots at deployment. Each request uses its exact
saved image independently, allowing old and new runs to validate concurrently. Missing or
unapproved images stop validation; the runner never substitutes a different image. See
[image permissions and retention](VALIDATION_SANDBOX.md#image-identity-and-deployment).
Missing infrastructure is not permission to substitute a new execution configuration.
An existing run also keeps its recorded workspace path. A missing workspace or a path outside
the saved root stops recovery instead of cloning a replacement and reusing old evidence.

Snapshots are configuration identities, not complete reproducible builds. Controller code,
unpinned executable dependencies in standalone process mode, external model service behavior,
tracker data, repository contents, tool/MCP services, and undeclared skill packages are not
captured artifacts. Process mode also retains its host configuration behavior; Docker is the
supported isolation boundary and applies the saved portable model settings. Full release
manifests, skill/tool package digests, explicit configuration migrations, execution image
retention, and policy revocation administration remain on the factory plan.

## Verification

Snapshot tests cover prompt/configuration/default identity, secret reference handling, concurrent
task settings, catalog changes, queued and active workflow reloads, original retry limits,
legacy refusal, host-fallback refusal, and node binding to the original version. Existing lease,
validation, publication, and contribution recovery suites remain applicable.

Deployment runs a read-only check after migration and startup:

```bash
docker compose exec -T tempo python < scripts/check-run-snapshots.py
```

It verifies stored complete snapshots and task-local defaults, rejects a changed in-memory copy,
and reports the count of legacy records without changing or dispatching them.
