# Agent account registry

The first account-management milestone adds **Agents & accounts** at `/agents/accounts/` and a
staff-only `/api/v1/accounts` API. Operators can name multiple Codex connections, grant active
projects in the same organization access, disable a connection, inspect recent audit events and
check the stored login file. Every mutation checks the current revision; stale forms fail without
replacing newer changes. Staff is currently an installation-wide administrative role.

Native Codex account routing is available for isolated Docker workflow agents. Grant a project
access, check the stored login, then choose a connection under **Agent assignments**. New runs pin
its UUID, project, credential generation and identity fingerprint in execution snapshot schema 2.
Unassigned configurations retain schema 1 and their existing installation behavior. Historical
runs keep their saved bindings. Assignment forms reject stale workflow digests and account revisions.
The API reports `runtime_routing: "codex_docker"` and `authentication_verified: false`; neither a
local cache check nor an app-server session start proves provider entitlement.

Each assigned session rechecks the live grant, enabled state, credential generation and local
identity before starting. It checks again before each turn. Revocation does not interrupt a turn
already in progress. A missing or changed login blocks execution without falling back to another
connection. Account-specific task homes receive only the selected credential file; refreshed files
are retained only if their identity matches. Authentication overrides and custom runtime commands
are rejected for assigned profiles. A resume requires a previously recorded thread with the same
run, node and account binding. Product run details show connection/model/session attempt attribution.

## Supported login setup

This slice supports provisioned **Codex ChatGPT file credentials** and an explicit reference to the
**existing installation login**. It does not implement a browser/device login flow, API-key
connections, credential replacement, token-refresh synchronization or Claude authentication.
OpenAI documents separate ChatGPT subscription and usage-billed API authentication, file storage
under `CODEX_HOME`, and copying the login cache to a trusted headless installation:
[official Codex authentication documentation](https://learn.chatgpt.com/docs/auth).

For the current installation login, add a connection with that source and select **Check stored
login**. This reads its private `auth.json` without copying, overwriting or signing out that login.
It does not automatically grant any project access.

For a separate login, add a **Provisioned Codex ChatGPT login** connection. Its UUID and revision
appear under connection details. An administrator supplies a private Codex `auth.json` through a
secure server-side channel, then runs the installed management command as the Tempo service user:

```sh
python manage.py provision_codex_account \
  --account CONNECTION_UUID --revision 1 \
  --auth-file /private/path/auth.json --operator STAFF_USERNAME
```

In Docker, invoke this with `docker compose exec -T --user 10001 tempo` after making the file
available to that user with mode `0600`. Supply only the path; never paste credential contents into
the application, command arguments, workflow files, tickets or chat. Remove the temporary source
using your installation's credential-handling procedure after successful provisioning.

The command copies only the credential file to `TEMPO_ACCOUNT_CREDENTIAL_ROOT`, under a UUID-derived
filename, with mode `0600`. Compose configures `/data/account-credentials` in a dedicated volume
mounted only into the control-plane service. This volume is not mounted into agent, validation,
acceptance, preview or release containers. Database rows and audit events contain metadata only.
The ordinary database backup does not back up this separate credential volume.

Provisioning refuses links, non-regular files, group/world-readable files, files larger than 1 MiB,
unsupported cache formats and overwrites. A successful import is immutable in this slice. If a
filesystem write succeeds but its database transaction fails, the orphan file remains private and
blocks another import; an administrator must reconcile it. No automatic overwrite is attempted.

## Meaning of a check

**Login stored** means the cache has supported fields and its locally reported workspace/user
reference matches the connection's recorded fingerprint. It does not verify signatures, token
expiry, subscription entitlement, provider reachability or available runtime capacity. The page
labels provider/runtime verification as pending. A missing, unreadable or changed identity produces
**Login unavailable** without displaying raw errors or credentials. Replacing an account identity
requires a new connection. Only a short fingerprint is displayed, not tokens or raw account claims.

## API

Authenticated staff can GET `/api/v1/accounts`. POST accepts exactly one of these shapes:

```json
{"action":"create","organization_id":1,"label":"Codex review","auth_mode":"codex-file"}
```

```json
{"action":"settings","account_id":"CONNECTION_UUID","revision":1,"disabled":false,"project_ids":[1]}
```

```json
{"action":"check","account_id":"CONNECTION_UUID","revision":2}
```

Cookie-authenticated requests require CSRF protection; valid bearer authentication uses the existing
API policy. Responses are private/no-store. Unsupported fields, including credential contents,
are rejected. Grant removal retains inactive grant rows and an audit event; records are not deleted.

## Assignments API

POST `/api/v1/accounts` as a staff operator:

```json
{"action":"assign","project_id":1,"profile":"verifier","account_id":"CONNECTION_UUID","revision":3,"configuration_digest":"CURRENT_EXECUTION_SNAPSHOT_DIGEST"}
```

Use an empty `account_id` and revision `0` to explicitly restore the installation default for new
runs. The UI supplies the current digest and connection revision. Agent profile edits through the
generic platform API also require staff access. Assignment and unassignment events record the
profile, project and resulting snapshot digest without storing credentials.

## Remaining integration gates

This slice does not synchronize refreshes back to the shared credential store, implement account
capacity leases, refresh-token rotation/reconnect, draining, provider cooldowns or automatic
fallback. Each task retains its own refreshed cache. Shared-login refresh contention and expiry
still require the scheduling/recovery workstream. The legacy independent publication reviewer
continues to use its installation configuration; product graph verifier profiles support routing.
Claude and embedded login setup remain planned.

Tests exercise simultaneous distinct account contexts and durable thread attribution, selected
credential seeding, wrong-identity rejection, disabled/revoked/missing connections, foreign-thread
resume rejection, immutable old runs and stale assignment requests. These use synthetic credentials;
they do not claim two real provider subscriptions completed model turns. The deployment smoke checks
separately cover the native Codex app-server handshake without model work.

## Validation

The regression suite passed 724 tests with 25 environment-dependent skips. Account tests passed
against isolated PostgreSQL, and browser checks at 390px and 1440px verified saving and clearing an
assignment without horizontal overflow. A final cleanup-status regression separately checks that
failed container cleanup is recorded as a failure. The deployment script runs concurrent synthetic
account-home isolation checks in Docker, alongside the native Codex handshake and existing
snapshot, build, preview, acceptance and release checks.
