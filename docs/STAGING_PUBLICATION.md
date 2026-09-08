# Publish retained builds to local staging

From a saved build's **Release readiness** page, approve its staging configuration, complete the
reviewed browser checks, check preview health, and rehearse rollback. When all six gates pass,
review the destination checkbox and choose **Publish to local staging**. The page shows progress,
a link to the published site, release history, and downloadable evidence. Each target keeps its
stable localhost address. This adapter publishes on the current Docker host, not a public cloud.

Migration `0022_release_publication` adds `ReleaseDeployment`: durable request identity,
configuration/target/artifact references, retained bundle identity, authorizing evidence, worker
lease, publication health receipt, and recovery receipt. Historical build manifests remain intact.
Readiness stays version 4; release evidence is a separate authenticated export with a digest.
A published release remains visible even when readiness for a new publication becomes blocked
because the target baseline changed or preview health expired.

## Publication boundary

Queueing requires the latest approved configuration, current scope and candidate checks,
passing reviewed acceptance, fresh preview health, and a passing target-bound rollback rehearsal.
The request includes an expected target fingerprint and idempotency key. Repeating the same
request after a lost response returns its existing record, including after publication; it cannot
publish again. One pending publication/recovery is allowed per target. Rehearsals and publications
also exclude each other while a target operation is pending.

The queue intent is committed before filesystem effects. The worker prepares the retained ZIP
without rebuilding it, serves that bundle at a private preflight address, and verifies the root
and every file over HTTP. It then locks the brief, artifact, target, and release in that order,
rechecks the six gates and their exact evidence IDs/digests, verifies the preflight bundle, and
switches the named target with an expected-operation compare-and-swap. The filesystem adapter
checks the lease and evidence expiry again after its archive verification, immediately before
replacing the pointer. Brief/plan edits and new artifact-scoped checks cannot race this switch
through the supported APIs.

After activation, the worker checks every file through the named target and revokes the private
preflight address. Only a matching health receipt, cleanup observation, unchanged artifact and
current evidence, matching authorization digest, and current lease can record `published`.
The rehearsal receipt is verified against the saved pre-activation baseline at completion,
because this publication has intentionally changed the target state. Health receipts include
worker revision and observation time; they are not continuous uptime monitoring.

The worker reads the preview volume read-only to validate preview evidence. It still has no
preview network connection, Docker CLI, model/tracker grants, agent homes, or control API route.
Its fresh network probes go only to the operator-configured staging server. The staging volume
is writable to the trusted worker; the serving container keeps a read-only copy and outbound-deny
firewall. Live probes verify both mounts and denied control API/execution-daemon connections.

## Rollback and interrupted work

**Roll back this release** restores the retained previous bundle, or removes the site when this
was the first release. It queues a durable recovery request and verifies the resulting HTTP
state. The successful publication receipt remains in history alongside its recovery receipt.
After restoring an earlier bundle, the UI links to the restored site using verified recovery
evidence. The original release record remains historical; it is not rewritten to fabricate a
new publication receipt for the restored pointer.

A failed post-activation check or expired worker lease also queues recovery. An interrupted
publication is conservatively rolled back rather than promoted using stale observations. Recovery
recognizes the queued baseline, this release's activation pointer, or its own restoration pointer.
It revalidates the previous bundle, restores only a recognized state, removes preflight access,
and checks the target over HTTP before clearing the target's operation lock. A crash after the
filesystem switch but before the database transaction commits is recoverable from the earlier
queue intent. Repeated restoration can reconcile a lost response.

Unexpected target ownership is never overwritten. Missing/corrupt previous bundles, changed
installation settings, failed health checks, or unrecognized pointers keep the target blocked.
Recovery retries up to three claims, with a 30-second delay after a failed attempt. **Retry
recovery** is available once operator attention is required. It retains the release record and
records the latest recovery requester/time. Operators must correct the service/storage/target
problem; retrying does not bypass identity checks. Worker deadlines are three minutes and stale
owners cannot authorize another write or accept a late completion.

## Retention and limits

Prepared publication bundles are retained even after failed publication or rollback. This keeps
recovery material available and avoids deleting a bundle whose activation response was lost.
Automated retention, storage quotas, and cleanup of abandoned temporary preparation directories
remain future work. Back up the release volume together with the database before relying on it
for durable product releases. A database-only restore does not reconstruct serving pointers or
retained files.

The supported profile is the browser-only React mini-app: no server processes, secret injection,
runtime variables, or database migrations. This is an installation-wide operator workflow on one
Docker host. Public deployment adapters, project-specific roles, continuous health monitoring,
and distributed operation/audit hardening remain outside this slice. A page open during a bundle
switch may need reloading if an old asset path is absent from the replacement.

This completes publication and rollback for an already retained, verified static build. It does
not establish that a fresh brief has autonomously produced a correct application, or that the
broader software factory plan is complete.

## Verification

The full suite passed with 613 tests and 22 environment-dependent skips. A separate PostgreSQL
run passed all 71 publication, rehearsal, configuration, and preview-health tests, including
concurrent target reservation. Fault tests cover interrupted preparation/activation/cleanup,
a filesystem switch followed by a database rollback, stale leases and evidence, failed deployed
health, recovery retries, and unexpected target ownership.

A disposable PostgreSQL/browser fixture used the retained React mini-app build from source
commit `5b11540` (ZIP SHA-256
`61d29496dd52f235587c6536df199fda74495b465d8728e5b287a74b2c5f4fd3`). Its reviewed
photo-to-SVG browser journey passed. Publication through the UI and constrained Docker worker
verified all 26 files (69,988,236 uncompressed bytes), and the published page opened in a browser.
First-release rollback returned HTTP 404 and retained both publication and recovery receipts.
The release page had no horizontal overflow at a 390-pixel viewport. This fixture supplied
upstream candidate records; it was not a fresh autonomous coding run or a production release.
