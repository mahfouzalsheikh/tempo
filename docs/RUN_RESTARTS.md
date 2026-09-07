# Fresh run restarts

Use **Needs attention → Restart with current configuration** for an older run without a
snapshot, a stopped run whose saved environment is no longer available, or work that should
start over under the current workflow. Failed and cancelled runs now appear in this list.
The dialog names the current configuration and explains that it starts fresh work.

A restart creates a successor run. It does not reconstruct or edit a historical snapshot.
The successor receives the active workflow version, a complete independent execution snapshot,
a unique checkout directory, and a new run identity for agent homes and Git publication.
Hooks create the checkout using the current configuration. No previous thread, task result,
checkpoint, approval, validation, feedback, or publication evidence is copied. Issue identity
and priority are retained; the worker still checks current issue state, routing, and mandatory
validation policy before executing. Configure the target repository's required checks before
restarting publication workflows.

The old run is marked cancelled / `SupersededByRestart`. Its errors, snapshots, results,
validation records, and workspace reference remain available in administration history. Pending
approvals are cancelled. Its queue idempotency key moves to the successor so normal polling
finds that run instead of creating duplicate work. The successor's `restarted_from` link and
an operator action record identify the transition. Existing files are not copied or deleted
by the restart operation; normal workspace retention and cleanup rules still apply.

## API and concurrency

`POST /api/v1/runs/{run_id}/restart` requires authentication and a JSON body:

```json
{"expected_snapshot_digest": "<restart_snapshot_digest from GET /api/v1/control>"}
```

Supply an `Idempotency-Key` to replay a request safely. Successful responses name the new run;
the operator action payload also records `successor_run_id`. A changed configuration digest
requires refreshing and confirming again. Reusing the key for a different run, user, action,
or configuration is rejected. Distinct restart requests cannot create two successors.

Restart is allowed for unowned queued, retry-scheduled, paused, failed, or cancelled runs.
Stop active work first. A worker lease, another active run for the issue, a completed run, or
an already superseded source prevents restart. GitHub repository changes are rejected rather
than interpreting an old issue in a different repository. Historical records without a known
repository target require separate onboarding rather than an inferred target.

Source row locks serialize restart with worker claims and approval decisions. The source update,
successor creation, pending approval cancellation, and audit record commit in one transaction.
Superseded runs cannot be revived by the supported retry or approval endpoints. A successor may
itself be restarted after stopping. This forms an auditable chain of distinct executions.

## Limits and checks

Ordinary resume/retry continues to use the original snapshot. Fresh restart intentionally
abandons that execution's progress. In-place snapshot schema migration, automatic conflict
repair, and retaining/routing multiple validation images remain separate work. If a fresh
checkout exists without a saved workspace record after interruption, execution stops for
another explicit restart instead of silently adopting potentially incomplete files.

Tests cover preserved history, fresh checkout and prompt execution, empty successor evidence,
legacy work visibility, confirmation identity, lease rejection, superseded controls, and
PostgreSQL races between duplicate restarts and worker claims. Deployment snapshot checks
verify migration and successor lineage without restarting production work.
