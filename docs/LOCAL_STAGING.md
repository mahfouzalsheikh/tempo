# Local staging configuration and adapter

From a saved build's release-readiness page, choose **Configure local staging**, name the
target, review the static application contract, and approve it. Reusing a target name within
the project reserves the same address for future builds. Migration
`0020_release_configuration` adds project-scoped `DeploymentTarget` and append-only
`ReleaseConfiguration` records. Concurrent approvals for one build require the latest
configuration ID; stale submissions cannot replace an unseen approval.

The `local-static-v1` adapter supports the exact `react-mini-app-v1` build profile. Its frozen
configuration includes the artifact, manifest, snapshot and build-profile digests; target
identity and origin; browser policy; health and rollback policies; and an explicit contract
with no server processes, runtime variables, injected secrets, or database migrations.
Changing a saved identity, serving port, policy, or artifact invalidates the configuration
gate until reviewed again. Authentication, CSRF protection, artifact scope, and private
response headers apply to the configuration form and readiness export.

Reviewed configuration and the static adapter now support [recorded rollback rehearsals](ROLLBACK_REHEARSAL.md).
The readiness gates can all pass for local staging. Publishing through the UI remains unavailable
until durable promotion intent/history, transactional evidence rechecks, activation recovery, and
post-promotion health checks are connected. Adapter smoke tests remain infrastructure checks;
only an identity-bound rehearsal job supplies product rollback evidence.

## Serving boundary

The eight-service Compose stack includes `release-server`, listening on host loopback port 8032
(override with `TEMPO_RELEASE_PORT`). Each target receives an independent
`http://<random-slot>.localhost:8032` origin. These addresses are accessible on the Docker host;
they are not public websites. Target names cannot select arbitrary URLs, shell commands, or
filesystem paths. Existing Amplify applications and DNS are unaffected by this adapter.

Tempo writes `/data/releases` through the `tempo-releases` named volume; the serving container
mounts only that volume, read-only. It has a read-only root, resource limits, an unprivileged
UID 10001 process with zero capabilities, and an outbound-deny firewall. Its separate ingress
network contains the serving container and dedicated rehearsal worker. No database, Docker, model, or tracker authority
is installed in that image. Requests use the same constrained browser policy and per-file
integrity checks as previews. Web Workers, WebAssembly and downloads are supported; external
connections, embedded pages and service workers are denied.

## Adapter operations

`tempo.release_files.prepare` validates the ZIP digest and complete inventory, writes a
new immutable bundle, fsyncs it, and publishes its directory atomically. Reusing a bundle ID
fails instead of overwriting an existing release. Bundles have no preview expiry.

`activate` takes a target slot, prepared bundle/configuration identity, expected current
operation ID, and fresh operation ID. A per-target filesystem lock serializes activations.
After revalidating the bundle, it atomically replaces the target pointer. A retry of the
currently active operation reconciles a lost response; a competing or older expectation
fails. Restoring a previous retained bundle uses a new operation with the current expectation.
The serving process rechecks the pointer after reading each response, rejecting a response
whose target changed during the read. A page already open during activation may still need
reloading if an old asset path is absent from the new release.

These primitives are host-internal and are not an authorization API or a complete promotion
ledger. The rehearsal coordinator records intent and recovers its temporary resources. A future
promotion coordinator must persist intent and reconcile interrupted activation at the named target. Bundles currently remain retained; deletion and retention policies
must protect active and rollback references. Back up the release volume with database backups
before relying on it for durable product releases.

`scripts/restart-tempo.sh` builds and starts the new service and runs
`scripts/check-release-target.py`. That check uses temporary random identities, exercises
activation, replacement, rollback, lost-response retry and stale-operation rejection over the
published host port, verifies process/storage/network isolation, then removes its own files.
It creates no product records and launches no coding agents.

## Verification for this step

The general suite passed 566 tests (20 environment-specific skips). The PostgreSQL release
lane passed 109 tests, including competing configuration approvals. A disposable PostgreSQL
and browser rehearsal approved a target for the retained React mini-app, passed its image-to-SVG
journey and all 26 preview file checks, and showed five passing readiness gates. Rollback
remained blocked. The configuration page fit a 390-pixel viewport without horizontal overflow.
These fixtures create no production product records.
