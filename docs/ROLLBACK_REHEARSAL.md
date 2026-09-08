# Recorded rollback rehearsal

A saved build's release-readiness page now offers **Rehearse rollback** after its staging
configuration is approved. The action queues a durable `RollbackRehearsal` record, bound to
that configuration, artifact, named target, storage location, HTTP endpoint, and current
previous release. Migration `0021_rollback_rehearsal` adds the job and its receipts.

The rehearsal uses a fresh private address on the staging service. It never activates or
withdraws the named target. When a previous release exists, Tempo verifies its retained ZIP
against its reviewed configuration and candidate evidence before using it as the recovery
baseline. Unrecognized or corrupt target state blocks the rehearsal.

The dedicated worker:

1. Revalidates the candidate/configuration and prepares an immutable temporary bundle.
2. Copies the previous target state to the temporary address, if a previous release exists.
3. Activates the candidate there and checks the root and every inventory file over HTTP.
4. Restores the previous bundle and checks all its files, or withdraws the first release and
   verifies an HTTP 404 with the required browser headers.
5. Removes the temporary address and candidate bundle, verifies withdrawal over HTTP, then
   persists a passing receipt after a final identity and lease check.

The report records candidate, restoration, and cleanup observations, the worker revision,
completion time, and a content digest. It does not export the private rehearsal address.
Receipts satisfy only the rollback readiness gate. Browser acceptance, current approved scope,
required candidate checks, reviewed configuration, and fresh preview health remain independent.
Version 4 readiness exports include verified rehearsal evidence. All six gates can pass
for local staging; [publication controls](STAGING_PUBLICATION.md) now use those gates.

## Concurrency and recovery

Only one queued, running, or cleanup-pending rehearsal may exist per target. A target row lock
and database uniqueness constraint enforce this across builds. Request keys reconcile repeated
submissions; stale configuration IDs/digests and conflicting request identities are rejected.

Workers claim with a UUID lease and a three-minute deadline. Artifact, target, then attempt row
locks fence activation and completion. The lease is checked again after expensive identity
verification and before filesystem effects. A stale worker cannot perform another activation or
accept a late report. Each stage has a deterministic operation ID, allowing a repeated effect
to reconcile a lost response while that operation remains current.

After an interrupted worker, recovery revokes only the job's private address and deletes only
its temporary candidate bundle. It retains the named target and its previous bundle. Cleanup
runs under the attempt lock, so it cannot race a stale worker's next effect. Cleanup failure
keeps the target blocked and retries after 30 seconds. Recovery records interruption rather than
inventing a successful rehearsal or resuming old health observations. Filesystem lock files are
retained because unlinking a lock inode could defeat serialization; retention/sweeping of old
records and lock files remains future work.

A passing observation expires after 24 hours. A newer rehearsal, changed configuration,
changed target pointer, changed baseline bytes, changed endpoint/storage settings, or tampered
report invalidates its use in readiness. Preview health still expires after five minutes.
The publication coordinator locks and freshly rechecks evidence; the read-time assessment is not
transactional publishing authorization. Rehearsals do not prove database recovery because this
supported static profile has no database migrations or server runtime.

## Worker and HTTP boundaries

Compose now has eight services. `release-worker` uses a dedicated Python image without Codex,
Docker CLI, agent homes, or model/tracker environment grants. It runs unprivileged with zero
capabilities, a read-only root, bounded resources, one writable release-volume mount, and read-only preview evidence.
It has a private connection to PostgreSQL and a connection to the staging server. It does not
join the control API/execution-daemon network. The serving container keeps its read-only mount
and outbound-deny firewall, including denial of new connections to the worker.

Deployment drains consumers and backs up PostgreSQL before applying the extra database network
attachment. The database's existing named volume is retained. Live probes verify the worker's
serving/withdrawal route, mounts and capabilities, and denied control API/daemon connections.
No production product records or coding-agent turns are created by those probes.

Serving and health checking now use Python's built-in MIME table, excluding host MIME overrides.
A real cross-container rehearsal found that `sitemap.xml` was classified differently on the host
and minimal worker image. The shared table removes that discrepancy. `preview-health-v2`
invalidates older v1 health receipts and requires a fresh check; historical evidence is retained.
Withdrawal checks reject accessible or redirected responses, cookies, duplicate headers,
oversized responses, partial bodies, and timed-out reads.

## Verification for this step

The full suite passed 595 tests (21 environment-specific skips). A final PostgreSQL lane passed
72 tests covering rehearsal races, stale ownership, lost-response retries, cleanup failure,
changed targets/configuration, corrupt baselines, and restoration of a different previous build.
The dedicated read-only worker image processed the retained mini-app's 26-file rehearsal against
a disposable PostgreSQL database and HTTP server. After its image-to-SVG acceptance journey and
preview health check, the UI showed six passing gates with no horizontal overflow at 390 pixels.
No production product records were created by this fixture. Publication was added in the
subsequent [publication slice](STAGING_PUBLICATION.md).
