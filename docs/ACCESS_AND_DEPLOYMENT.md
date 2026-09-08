# Access control and test deployments

Operational pages (`/`, `/ops/`, and `/ops/configuration/`) now require sign-in. Anonymous page
requests redirect to login with the original return path. Operational APIs, including state,
configuration, issue detail, approvals, controls, and the event stream, return HTTP 401 before
reading runtime state when no active user is authenticated. Existing session, JWT cookie, and
Bearer authentication continue to work. Responses use `Cache-Control: no-store` and vary by
Cookie and Authorization. Login, static assets, and the minimal health endpoint remain public.

Event streams reconnect at least every five minutes to recheck access and stop at JWT expiry.
User deactivation or session revocation can therefore take up to five minutes to close an already
open stream. This is installation-wide operator access; per-project membership and role policy
remain planned work. The UI reports an expired session and links back to login.

## Validation runner

Both `/run` and `/run-stream` require a separate Bearer credential. Authentication is checked
before reading the body or resolving the workspace. Missing/invalid credentials return 401;
missing server configuration makes jobs and runner health return 503. No unauthenticated
execution compatibility mode is provided.

Set `TEMPO_VALIDATION_RUNNER_TOKEN` to the same random value on the control plane and runner.
The value must contain at least 32 non-whitespace ASCII characters. The local provisioning script
generates a 48-byte random credential in the ignored `.env` file, retains it across deployments,
and never prints it. Newly written files have mode 0600. Back up or rotate this credential through
the deployment's secret-management process. Restart both services together after rotation.

The host reads `validation.runner_token` as an environment reference, defaulting to
`$TEMPO_VALIDATION_RUNNER_TOKEN`. A custom host reference can be used if the server receives the
same value under its standard variable. The resolved value is sent only in Authorization, outside
job payloads and saved configuration. The client disables environment proxies and redirects;
non-success response bodies are not copied into validation history. Runtime grants cannot expose
the standard or configured runner credential names to coding agents.

Requests contain workspace, command, timeout_ms, and max_output_chars. Docker jobs must also
supply execution_image matching the runner's immutable image ID; a mismatch returns 409. Workspace
must be a directory strictly beneath the runner's root. Limits are one MiB for the body, ten
seconds to receive it, one hour per command, and one million retained output characters. The
runner rejects type coercions, root-directory execution, and extra environment fields.

The runner uses its configured `DOCKER_HOST` to create a disposable container for each command.
Project code receives neither Docker access nor the authentication token. The container mounts
only the requested workspace, has no network, and runs project code as UID 10001. Image identity
is bound to the request, final result, and validation policy. See [validation isolation](VALIDATION_SANDBOX.md).

The shared credential authenticates the caller, not an individual leased run. Per-job capability
tokens, control-plane network segregation, and trusted validation harnesses remain necessary.
Coding agents and hooks now use [isolated containers](RUNTIME_ISOLATION.md). The current private Compose network uses HTTP and a
privileged Docker execution service. Workload bridge traffic is now restricted by the
[execution network policy](EXECUTION_NETWORK.md); the control-plane network still has daemon access.
Use TLS for runner traffic crossing a trusted-host boundary.
Authentication alone does
not prevent a process with shared filesystem/process access from reaching credentials.

## Commit, push, deploy

Completed implementation steps are committed and pushed before deployment, following the user's
requested workflow. The local test stack is the existing Compose project; port selection remains
controlled by `TEMPO_PORT` in `.env`.

Run `./scripts/restart-tempo.sh` from a committed checkout. It:

1. Ensures a stable local runner credential is configured.
2. Builds the application services and the execution daemon's policy image.
3. Starts the existing database without forcibly recreating it.
4. Stops Tempo and validation gracefully and saves a private backup beneath `var/backups/`.
5. Updates execution infrastructure, waits for firewall-aware readiness, and provisions the agent network.
6. Loads the validation image into the daemon and supplies its immutable ID to both application services.
7. Starts the application services and waits for health checks. Startup applies migrations.
8. Verifies the deployed commit, protected reads, validation, network isolation, runtime resume,
   hook isolation, and initialization/login recognition with the installed Codex binary.
9. Checks product intake, retained builds, preview health, browser acceptance, and temporary
   staging activation/rollback with process, storage, and network isolation probes.

The stack now has eight services, including a separate [local staging server](LOCAL_STAGING.md)
and [rollback rehearsal worker](ROLLBACK_REHEARSAL.md).
Its loopback port defaults to 8032. Staging configuration approval is available from release
readiness, with durable rollback rehearsals available after approval and
[publication/rollback controls](STAGING_PUBLICATION.md) once the gates pass. Consumer shutdown and a database backup precede applying the private
PostgreSQL network attachment for the rehearsal worker; its data volume is preserved.

The script preserves named volumes. It does not run `down -v`, remove orphan services, or
automatically restore a database. It exits on failure. If an update fails after Tempo stops, fix
the reported startup issue or explicitly restore the previous application image. Database
restoration is a separate operator procedure; a backup may contain historical credentials and
must remain private. `/healthz` exposes the build revision so a tester can identify deployed code.

The browser regression scenario uses `tests/browser/serve.py`, a loopback-only fixture server
that directly renders templates without starting agents or accessing live application APIs.
It deliberately bypasses production authentication for mocked UI scenarios. Never deploy this
test server; use the regular Tempo entrypoint for the application.
